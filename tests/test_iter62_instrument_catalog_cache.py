"""Iteration 62 (D62-11b/11c): two-level CTP instrument catalog cache.

UNIT/FAKE and local multi-process evidence only: catalog queries come from
fakes; the shared level is a temporary directory.  No network, no account.
"""

from __future__ import annotations

import json
import multiprocessing
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from bt_api_ctp import instrument_catalog_cache as cache_module
from bt_api_ctp.direct_write_provider import CtpDirectWriteProvider
from bt_api_ctp.feeds.live_ctp_feed import CtpRequestDataFuture
from bt_api_ctp.instrument_catalog_cache import (
    CATALOG_CACHE_DIR_ENV,
    CtpFeedCatalogSource,
    InstrumentCatalogCache,
    InstrumentCatalogEntry,
    InstrumentCatalogNotReady,
    default_catalog_cache_dir,
    instrument_catalog_scope_key,
    project_catalog_entry,
)
from bt_api_ctp.query import QueryResult

NOW = datetime(2026, 11, 6, 1, 0, tzinfo=timezone.utc)
SCOPE = instrument_catalog_scope_key(
    broker_id="9999", td_front="tcp://182.254.243.31:30001", env_profile="simnow_set1"
)
OTHER_SCOPE = instrument_catalog_scope_key(
    broker_id="9999", td_front="tcp://182.254.243.31:30002", env_profile="simnow_set1"
)
DAY = "20261106"
NEXT_DAY = "20261109"


def _native(symbol, exchange, tick=1.0, multiple=10, **extra):
    row = {
        "InstrumentID": symbol,
        "ExchangeID": exchange,
        "InstrumentName": "name-not-cached",
        "ExchangeInstID": symbol,
        "ProductID": symbol.rstrip("0123456789"),
        "ProductClass": "1",
        "PriceTick": tick,
        "VolumeMultiple": multiple,
        "ExpireDate": "20270115",
        "IsTrading": 1,
        "InstLifePhase": "1",
        "DeliveryYear": 2027,
        "DeliveryMonth": 1,
        "LongMarginRatio": 0.1,
        "ShortMarginRatio": 0.1,
        "OptionsType": "\x00",
        "CombinationType": "\x00",
        "StrikePrice": 0.0,
        "UnderlyingInstrID": "",
    }
    row.update(extra)
    return row


CATALOG_ROWS = (
    _native("rb2701", "SHFE"),
    _native("ag2512", "SHFE", tick=1.0, multiple=15),
    _native("IF2612", "CFFEX", tick=0.2, multiple=300),
    {"InstrumentID": "", "ExchangeID": "SHFE"},  # unusable row
)


def _result(rows, *, generation=4, complete=True, timed_out=False, error_code=None):
    return QueryResult(
        request_type="instruments",
        request_id=3,
        connection_generation=generation,
        account_fingerprint="acct_test",
        started_at_utc=NOW,
        completed_at_utc=NOW,
        is_last_seen=complete,
        error_code=error_code,
        error_message="" if complete else "query_timeout",
        timed_out=timed_out,
        complete=complete,
        records=tuple(rows),
        completion_origin="query_response" if complete else "timeout",
    )


class FakeSource:
    """Picklable catalog source counting native requests."""

    def __init__(
        self,
        *,
        scope=SCOPE,
        day=DAY,
        generation=4,
        rows=CATALOG_ROWS,
        single_rows=None,
        delay=0.0,
        single_delay=0.0,
        complete=True,
        raise_full=False,
    ):
        self.scope = scope
        self.day = day
        self.generation = generation
        self.rows = tuple(rows)
        self.single_rows = dict(single_rows or {})
        self.delay = delay
        self.single_delay = single_delay
        self.complete = complete
        self.raise_full = raise_full
        self.full_calls = 0
        self.single_calls: list[str] = []
        self.on_query = None

    def catalog_scope(self):
        return self.scope

    def trading_day(self):
        return self.day

    def session_generation(self):
        return self.generation

    def query_instruments(self, *, instrument_id, timeout, max_duration):
        if instrument_id == "":
            self.full_calls += 1
            assert max_duration is not None
            if self.delay:
                time.sleep(self.delay)
            if self.raise_full:
                raise RuntimeError("native failure")
            rows = self.rows
        else:
            self.single_calls.append(instrument_id)
            if self.single_delay:
                time.sleep(self.single_delay)
            rows = self.single_rows.get(instrument_id, ())
        generation = self.generation
        if self.on_query is not None:
            self.on_query(self, instrument_id)
        return _result(rows, generation=generation, complete=self.complete)


def _cache(tmp_path, **kwargs):
    kwargs.setdefault("lock_timeout", 5.0)
    kwargs.setdefault("lock_poll_interval", 0.01)
    return InstrumentCatalogCache(tmp_path / "catalog", **kwargs)


def _files(tmp_path):
    root = tmp_path / "catalog"
    return sorted(str(path.relative_to(root)) for path in root.rglob("*") if path.is_file())


# ---------------------------------------------------------------------------
# levels and format
# ---------------------------------------------------------------------------
def test_full_query_then_memory_then_shared_file(tmp_path):
    source = FakeSource()
    cache = _cache(tmp_path)
    first = cache.lookup(source, "rb2701")
    assert (first.origin, first.venue, first.native_queries) == ("full_query", "SHFE", 1)
    second = cache.lookup(source, "IF2612")
    assert (second.origin, second.venue, second.native_queries) == ("memory", "CFFEX", 0)
    assert source.full_calls == 1 and source.single_calls == []
    assert cache.counters()["full_queries"] == 1
    assert cache.counters()["memory_hits"] == 1

    other_process = _cache(tmp_path)
    other_source = FakeSource()
    third = other_process.lookup(other_source, "ag2512")
    assert (third.origin, third.venue) == ("file", "SHFE")
    assert other_source.full_calls == 0
    assert other_process.counters()["file_hits"] == 1
    assert other_process.counters()["full_queries"] == 0


def test_published_file_format_path_and_no_secrets(tmp_path):
    cache = _cache(tmp_path)
    cache.lookup(FakeSource(), "rb2701")
    assert _files(tmp_path) == [f"{SCOPE}/{DAY}.json", f"{SCOPE}/{DAY}.lock"]
    path = cache.catalog_path(SCOPE, DAY)
    text = path.read_text("ascii")
    document = json.loads(text)
    assert document["format"] == "bt_api_ctp.instrument_catalog"
    assert document["version"] == 1
    assert document["scope"] == SCOPE and document["trading_day"] == DAY
    assert document["complete"] is True
    assert document["entry_count"] == 3 and document["skipped_rows"] == 1
    assert len(document["content_sha256"]) == 64
    assert [entry["instrument_id"] for entry in document["entries"]] == [
        "IF2612",
        "ag2512",
        "rb2701",
    ]
    # Metadata only: no names, margin ratios, broker/front or account values.
    for secret in ("9999", "182.254.243.31", "name-not-cached", "MarginRatio", "margin"):
        assert secret not in text
    if os.name != "nt":
        assert path.stat().st_mode & 0o077 == 0


def test_entries_hold_reference_metadata_only():
    entry = project_catalog_entry(
        _native("rb2701", "SHFE", tick=1.7976931348623157e308, OptionsType="\x00")
    )
    assert entry.instrument_id == "rb2701" and entry.exchange_id == "SHFE"
    assert entry.price_tick is None  # DBL_MAX means unset
    assert entry.volume_multiple == 10 and entry.is_trading is True
    assert entry.options_type is None and entry.strike_price is None
    assert entry.product_id == "rb" and entry.product_class == "1"
    assert not any("margin" in name or "name" == name[-4:] for name in entry.as_dict())
    assert project_catalog_entry({"InstrumentID": "rb2701", "ExchangeID": ""}) is None
    assert project_catalog_entry({"InstrumentID": "x" * 81, "ExchangeID": "SHFE"}) is None
    with pytest.raises(ValueError):
        InstrumentCatalogEntry(instrument_id="rb2701", exchange_id="SHFE", price_tick=float("nan"))


def test_default_cache_dir_is_overridable_and_under_the_user_cache(monkeypatch, tmp_path):
    monkeypatch.setenv(CATALOG_CACHE_DIR_ENV, str(tmp_path / "x"))
    assert default_catalog_cache_dir() == tmp_path / "x"
    monkeypatch.delenv(CATALOG_CACHE_DIR_ENV)
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    if os.name != "nt":
        assert default_catalog_cache_dir() == Path.home() / ".cache" / "bt_api_ctp" / "catalog"


# ---------------------------------------------------------------------------
# trading day, scope, corruption
# ---------------------------------------------------------------------------
def test_trading_day_roll_queries_the_new_day_and_never_serves_the_old(tmp_path):
    source = FakeSource()
    cache = _cache(tmp_path)
    cache.lookup(source, "rb2701")
    source.day = NEXT_DAY
    source.rows = (_native("rb2705", "SHFE"),)
    rolled = cache.lookup(source, "rb2705")
    assert (rolled.origin, rolled.trading_day, rolled.venue) == ("full_query", NEXT_DAY, "SHFE")
    assert source.full_calls == 2
    # rb2701 is not in the new day's catalog: queried on demand, not taken
    # from the previous day's memory or file.
    missing = cache.lookup(source, "rb2701")
    assert missing.origin == "supplement" and missing.venue is None
    assert source.single_calls == ["rb2701"]
    assert _files(tmp_path) == sorted(
        [f"{SCOPE}/{DAY}.json", f"{SCOPE}/{DAY}.lock", f"{SCOPE}/{NEXT_DAY}.json", f"{SCOPE}/{NEXT_DAY}.lock"]
    )


@pytest.mark.parametrize("day", [None, "", "2026-11-06", "2026116", 20261106])
def test_unknown_trading_day_is_not_ready_even_with_cached_data(tmp_path, day):
    source = FakeSource()
    cache = _cache(tmp_path)
    cache.lookup(source, "rb2701")
    source.day = day
    with pytest.raises(InstrumentCatalogNotReady) as info:
        cache.lookup(source, "rb2701")
    assert info.value.code == "ctp_catalog_trading_day_unknown"
    assert source.full_calls == 1


def test_previous_day_file_under_the_current_name_is_rejected(tmp_path):
    cache = _cache(tmp_path)
    cache.lookup(FakeSource(day=DAY), "rb2701")
    old = cache.catalog_path(SCOPE, DAY)
    new = cache.catalog_path(SCOPE, NEXT_DAY)
    new.write_bytes(old.read_bytes())
    fresh = _cache(tmp_path)
    source = FakeSource(day=NEXT_DAY)
    lookup = fresh.lookup(source, "rb2701")
    assert lookup.origin == "full_query" and source.full_calls == 1
    assert fresh.counters()["file_rejects"] == 1
    assert json.loads(new.read_text())["trading_day"] == NEXT_DAY


def _tamper(document):
    document["entries"][0]["price_tick"] = 99.0


@pytest.mark.parametrize(
    "corrupt",
    [
        lambda path: path.write_bytes(b"{not json"),
        lambda path: path.write_bytes(path.read_bytes()[:-40]),
        lambda path: _rewrite(path, _tamper),
        lambda path: _rewrite(path, lambda d: d.update(complete=False)),
        lambda path: _rewrite(path, lambda d: d.update(version=2)),
        lambda path: _rewrite(path, lambda d: d.update(scope=OTHER_SCOPE)),
        lambda path: _rewrite(path, lambda d: d.update(entry_count=2)),
        lambda path: _rewrite(path, lambda d: d["entries"][0].update(secret="x")),
    ],
)
def test_corrupted_file_is_rejected_and_rebuilt(tmp_path, corrupt):
    _cache(tmp_path).lookup(FakeSource(), "rb2701")
    path = _cache(tmp_path).catalog_path(SCOPE, DAY)
    corrupt(path)
    cache = _cache(tmp_path)
    source = FakeSource()
    lookup = cache.lookup(source, "IF2612")
    assert lookup.origin == "full_query" and lookup.venue == "CFFEX"
    assert source.full_calls == 1
    assert cache.counters()["file_rejects"] == 1
    assert _cache(tmp_path).lookup(FakeSource(), "rb2701").origin == "file"


def _rewrite(path, change):
    document = json.loads(path.read_text())
    change(document)
    path.write_text(json.dumps(document))


def test_scopes_are_isolated(tmp_path):
    cache = _cache(tmp_path)
    first = FakeSource(scope=SCOPE)
    second = FakeSource(scope=OTHER_SCOPE, rows=(_native("rb2701", "INE"),))
    assert cache.lookup(first, "rb2701").venue == "SHFE"
    assert cache.lookup(second, "rb2701").venue == "INE"
    assert first.full_calls == 1 and second.full_calls == 1
    assert {path.split("/")[0] for path in _files(tmp_path)} == {SCOPE, OTHER_SCOPE}
    assert SCOPE != instrument_catalog_scope_key(
        broker_id="9999", td_front="tcp://182.254.243.31:30001", env_profile="production"
    )
    with pytest.raises(ValueError):
        instrument_catalog_scope_key(broker_id="", td_front="tcp://x", env_profile="")
    unknown = FakeSource(scope=None)
    with pytest.raises(InstrumentCatalogNotReady) as info:
        cache.lookup(unknown, "rb2701")
    assert info.value.code == "ctp_catalog_scope_unknown"


# ---------------------------------------------------------------------------
# failures during the build
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "source,code",
    [
        (FakeSource(complete=False), "ctp_catalog_full_query_incomplete"),
        (FakeSource(raise_full=True), "ctp_catalog_full_query_failed"),
        (FakeSource(rows=({"InstrumentID": "", "ExchangeID": ""},)), "ctp_catalog_full_query_empty"),
    ],
)
def test_failed_build_is_not_ready_and_publishes_nothing(tmp_path, source, code):
    cache = _cache(tmp_path)
    with pytest.raises(InstrumentCatalogNotReady) as info:
        cache.lookup(source, "rb2701")
    assert info.value.code == code
    assert _files(tmp_path) == [f"{SCOPE}/{DAY}.lock"]
    assert cache.counters()["build_failures"] == 1
    good = FakeSource()
    assert cache.lookup(good, "rb2701").origin == "full_query"


def test_stale_generation_result_is_not_ready(tmp_path):
    source = FakeSource()
    source.on_query = None
    original = source.query_instruments

    def stale(**kwargs):
        result = original(**kwargs)
        return _result(result.records, generation=3)

    source.query_instruments = stale
    with pytest.raises(InstrumentCatalogNotReady) as info:
        _cache(tmp_path).lookup(source, "rb2701")
    assert info.value.code == "ctp_catalog_full_query_incomplete"


@pytest.mark.parametrize(
    "change",
    [
        lambda s: setattr(s, "day", NEXT_DAY),
        lambda s: setattr(s, "scope", OTHER_SCOPE),
        lambda s: setattr(s, "generation", 5),
    ],
)
def test_session_change_during_the_build_is_not_ready(tmp_path, change):
    source = FakeSource()
    source.on_query = lambda s, instrument: change(s)
    cache = _cache(tmp_path)
    with pytest.raises(InstrumentCatalogNotReady) as info:
        cache.lookup(source, "rb2701")
    assert info.value.code == "ctp_catalog_session_changed"
    assert not any(name.endswith(".json") for name in _files(tmp_path))


def test_publish_failure_leaves_no_temp_file_and_serves_this_process(tmp_path, monkeypatch):
    def broken_replace(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(cache_module.os, "replace", broken_replace)
    cache = _cache(tmp_path)
    source = FakeSource()
    assert cache.lookup(source, "rb2701").venue == "SHFE"
    assert cache.counters()["publish_failures"] == 1
    assert _files(tmp_path) == [f"{SCOPE}/{DAY}.lock"]  # no .json, no .tmp
    assert cache.lookup(source, "ag2512").origin == "memory"


def test_stale_temp_files_of_dead_builders_are_removed(tmp_path):
    directory = tmp_path / "catalog" / SCOPE
    directory.mkdir(parents=True)
    (directory / f".{DAY}.dead.tmp").write_text("partial")
    (directory / f".{NEXT_DAY}.other.tmp").write_text("other day")
    _cache(tmp_path).lookup(FakeSource(), "rb2701")
    assert _files(tmp_path) == sorted(
        [f"{SCOPE}/.{NEXT_DAY}.other.tmp", f"{SCOPE}/{DAY}.json", f"{SCOPE}/{DAY}.lock"]
    )


def test_conflicting_rows_are_never_used(tmp_path):
    rows = (_native("rb2701", "SHFE", tick=1.0), _native("rb2701", "SHFE", tick=2.0))
    source = FakeSource(rows=rows + (_native("ag2512", "SHFE"),))
    lookup = _cache(tmp_path).lookup(source, "rb2701")
    assert lookup.conflicted is True and lookup.venue is None and lookup.entries == ()
    assert source.single_calls == []
    assert json.loads(_cache(tmp_path).catalog_path(SCOPE, DAY).read_text())["conflicts"] == [
        "rb2701"
    ]
    assert _cache(tmp_path).lookup(FakeSource(), "rb2701").conflicted is True


# ---------------------------------------------------------------------------
# on-demand supplement
# ---------------------------------------------------------------------------
def test_missing_instrument_is_queried_once_and_shared(tmp_path):
    source = FakeSource(
        single_rows={"sc2701": (_native("sc2701", "INE"), _native("rb2701", "SHFE", tick=5.0))}
    )
    cache = _cache(tmp_path)
    cache.lookup(source, "rb2701")
    lookup = cache.lookup(source, "sc2701")
    assert (lookup.origin, lookup.venue, lookup.native_queries) == ("supplement", "INE", 1)
    assert source.single_calls == ["sc2701"]
    assert cache.counters()["incremental_queries"] == 1
    assert cache.lookup(source, "sc2701").origin == "memory"
    # The foreign rb2701 row of the filtered answer was not merged.
    assert cache.lookup(source, "rb2701").entries[0].price_tick == 1.0
    other = FakeSource()
    shared = _cache(tmp_path).lookup(other, "sc2701")
    assert (shared.origin, shared.venue) == ("file", "INE")
    assert other.full_calls == 0 and other.single_calls == []


def test_unknown_instrument_is_not_cached(tmp_path):
    source = FakeSource()
    cache = _cache(tmp_path)
    assert cache.lookup(source, "zz9999").venue is None
    assert cache.lookup(source, "zz9999").venue is None
    assert source.single_calls == ["zz9999", "zz9999"]


def test_incomplete_supplement_is_not_ready_and_not_merged(tmp_path):
    source = FakeSource(single_rows={"sc2701": (_native("sc2701", "INE"),)})
    cache = _cache(tmp_path)
    cache.lookup(source, "rb2701")
    source.complete = False
    with pytest.raises(InstrumentCatalogNotReady) as info:
        cache.lookup(source, "sc2701")
    assert info.value.code == "ctp_catalog_query_incomplete"
    assert "sc2701" not in cache.catalog_path(SCOPE, DAY).read_text()


def test_concurrent_supplements_from_two_caches_lose_no_update(tmp_path):
    rows = {"sc2701": (_native("sc2701", "INE"),), "lu2701": (_native("lu2701", "INE"),)}
    _cache(tmp_path).lookup(FakeSource(), "rb2701")
    first, second = _cache(tmp_path), _cache(tmp_path)
    first.lookup(FakeSource(), "rb2701")
    second.lookup(FakeSource(), "rb2701")
    barrier = threading.Barrier(2)
    errors = []

    def run(cache, symbol):
        try:
            source = FakeSource(single_rows=rows, single_delay=0.05)
            barrier.wait(5)
            assert cache.lookup(source, symbol).venue == "INE"
        except Exception as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [
        threading.Thread(target=run, args=(first, "sc2701")),
        threading.Thread(target=run, args=(second, "lu2701")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert errors == []
    document = json.loads(_cache(tmp_path).catalog_path(SCOPE, DAY).read_text())
    assert {"sc2701", "lu2701", "rb2701"} <= {e["instrument_id"] for e in document["entries"]}


# ---------------------------------------------------------------------------
# cross-process coordination
# ---------------------------------------------------------------------------
def _hold_lock(cache, scope=SCOPE, day=DAY):
    lock = cache._open_file_lock(scope, day)
    assert lock.try_acquire() is True
    return lock


def test_lock_timeout_is_not_ready_and_issues_no_query(tmp_path):
    holder = _hold_lock(_cache(tmp_path))
    try:
        source = FakeSource()
        cache = _cache(tmp_path, lock_timeout=0.2)
        with pytest.raises(InstrumentCatalogNotReady) as info:
            cache.lookup(source, "rb2701")
        assert info.value.code == "ctp_catalog_lock_timeout"
        assert source.full_calls == 0
        assert cache.counters()["lock_timeouts"] == 1
    finally:
        holder.close()
    assert cache.lookup(source, "rb2701").origin == "full_query"


def test_waiter_uses_the_file_published_by_the_lock_holder(tmp_path):
    builder = _cache(tmp_path)
    holder = _hold_lock(builder)
    waiter = _cache(tmp_path)
    source = FakeSource()
    outcome = {}

    def wait():
        outcome["lookup"] = waiter.lookup(source, "rb2701")

    thread = threading.Thread(target=wait)
    thread.start()
    time.sleep(0.2)
    built = builder._build_full(FakeSource(), SCOPE, DAY, 4)
    assert builder._publish(built) is True
    thread.join(5)
    holder.close()
    assert outcome["lookup"].origin == "file"
    assert source.full_calls == 0
    assert waiter.counters()["lock_waits"] == 1


def _child_lookup(cache_dir, start, results, delay):
    source = FakeSource(delay=delay)
    cache = InstrumentCatalogCache(cache_dir, lock_timeout=30.0, lock_poll_interval=0.01)
    start.wait(30)
    lookup = cache.lookup(source, "rb2701")
    results.put((os.getpid(), source.full_calls, lookup.origin, lookup.venue, cache.counters()))


def test_two_concurrent_processes_issue_exactly_one_full_query(tmp_path):
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    results = context.Queue()
    cache_dir = str(tmp_path / "catalog")
    workers = [
        context.Process(target=_child_lookup, args=(cache_dir, start, results, 0.8))
        for _ in range(2)
    ]
    for worker in workers:
        worker.start()
    time.sleep(0.5)  # let both children import and block on the start event
    start.set()
    outcomes = [results.get(timeout=60) for _ in workers]
    for worker in workers:
        worker.join(30)
        assert worker.exitcode == 0
    assert sum(full for _pid, full, *_rest in outcomes) == 1
    assert sorted(origin for _pid, _full, origin, *_rest in outcomes) == ["file", "full_query"]
    assert {venue for *_head, venue, _counters in outcomes} == {"SHFE"}
    assert len({pid for pid, *_rest in outcomes}) == 2
    assert not any(name.endswith(".tmp") for name in _files(tmp_path))


def _child_abort_while_holding(cache_dir, acquired):
    cache = InstrumentCatalogCache(cache_dir)
    lock = cache._open_file_lock(SCOPE, DAY)
    assert lock.try_acquire()
    acquired.set()
    time.sleep(0.5)
    os._exit(3)  # builder dies without publishing or unlocking


def test_aborted_builder_releases_the_lock_and_publishes_nothing(tmp_path):
    context = multiprocessing.get_context("spawn")
    acquired = context.Event()
    cache_dir = str(tmp_path / "catalog")
    child = context.Process(target=_child_abort_while_holding, args=(cache_dir, acquired))
    child.start()
    assert acquired.wait(30)
    source = FakeSource()
    cache = _cache(tmp_path, lock_timeout=20.0)
    lookup = cache.lookup(source, "rb2701")
    child.join(10)
    assert child.exitcode == 3
    assert lookup.origin == "full_query" and source.full_calls == 1
    assert cache.counters()["lock_waits"] == 1


# ---------------------------------------------------------------------------
# provider wiring
# ---------------------------------------------------------------------------
class _Trader:
    connection_generation = 4

    def __init__(self):
        self.day = DAY

    def add_execution_listener(self, listener):
        pass

    def get_session_state(self):
        return {
            "front_id": 3,
            "session_id": 99,
            "trading_day": self.day,
            "connection_generation": self.connection_generation,
        }


def _provider(tmp_path, answer, **cache_kwargs):
    feed = CtpRequestDataFuture(
        broker_id="broker-test",
        user_id="investor-test",
        password="synthetic-only",
        td_front="tcp://127.0.0.1:19991",
        md_front="tcp://127.0.0.1:19992",
        auto_settlement_confirm=False,
    )
    trader = _Trader()
    feed._trader = trader
    calls = []

    def query_instruments_result(**kwargs):
        calls.append(kwargs)
        return answer(kwargs)

    feed.query_instruments_result = query_instruments_result
    cache = _cache(tmp_path, **cache_kwargs)
    return CtpDirectWriteProvider(feed, catalog_cache=cache), trader, calls, cache


def _filtered(rows):
    """A front answering the single-instrument query with that instrument only."""

    def answer(kwargs):
        wanted = kwargs["instrument_id"]
        return _result(tuple(r for r in rows if not wanted or r.get("InstrumentID") == wanted))

    return answer


def test_order_path_never_starts_the_full_catalog_query(tmp_path):
    """SimNow 2026-11-06 (I62-12a): a full catalog query inside an order outlasted
    its budget, held the session's only query slot, and the order was refused
    ``ctp_direct_exchange_id_required``. Without a catalog of the day an order
    resolves its venue with the single-instrument query."""

    provider, _trader, calls, cache = _provider(tmp_path, _filtered(CATALOG_ROWS))
    assert provider.resolve_exchange_id("rb2701") == "SHFE"
    assert provider.resolve_exchange_id("IF2612") == "CFFEX"
    assert provider.resolve_exchange_id("zz9999") is None
    assert [kw["instrument_id"] for kw in calls] == ["rb2701", "IF2612", "zz9999"]
    assert provider.last_catalog_error == "ctp_catalog_not_built"
    counters = provider.instrument_catalog_counters()
    assert counters["full_queries"] == 0 and counters["incremental_queries"] == 0
    assert counters["provider_native_queries"] == 3
    source = CtpFeedCatalogSource(provider._feed)
    assert not cache.catalog_path(source.catalog_scope(), DAY).exists()


def test_warmed_catalog_serves_orders_and_supplements_missing_instruments(tmp_path):
    provider, _trader, calls, cache = _provider(tmp_path, _filtered(CATALOG_ROWS))
    ready = provider.warm_instrument_catalog()
    assert ready.origin == "full_query" and ready.entry_count == 3
    assert [kw["instrument_id"] for kw in calls] == [""]
    assert calls[0]["max_duration"] == 90.0
    assert provider.resolve_exchange_id("rb2701") == "SHFE"
    assert provider.resolve_exchange_id("IF2612") == "CFFEX"
    assert provider.resolve_exchange_id("zz9999") is None  # queried once, on demand
    assert [kw["instrument_id"] for kw in calls] == ["", "zz9999"]
    counters = provider.instrument_catalog_counters()
    assert counters["full_queries"] == 1 and counters["incremental_queries"] == 1
    assert counters["memory_hits"] == 3 and counters["provider_native_queries"] == 2
    assert provider.last_catalog_error is None
    source = CtpFeedCatalogSource(provider._feed)
    assert cache.catalog_path(source.catalog_scope(), DAY).is_file()


def test_order_path_reuses_a_catalog_published_by_another_process(tmp_path):
    builder, _t1, _c1, cache_one = _provider(tmp_path, _filtered(CATALOG_ROWS))
    builder.warm_instrument_catalog()
    # Another process: a fresh in-process cache over the same shared directory.
    provider, _t2, calls, _cache_two = _provider(tmp_path, _filtered(CATALOG_ROWS))
    assert provider.resolve_exchange_id("ag2512") == "SHFE"
    assert calls == []  # read from the published file, no native query
    assert provider.instrument_catalog_counters()["file_hits"] == 1


def test_failed_warm_up_leaves_orders_on_the_single_query(tmp_path):
    def answer(kwargs):
        if not kwargs["instrument_id"]:
            return _result(CATALOG_ROWS, complete=False, timed_out=True)
        return _result((_native("rb2701", "SHFE"), _native("ag2512", "SHFE")))

    provider, _trader, calls, _cache_ = _provider(tmp_path, answer)
    with pytest.raises(InstrumentCatalogNotReady) as error:
        provider.warm_instrument_catalog()
    assert error.value.code == "ctp_catalog_full_query_incomplete"
    assert provider.resolve_exchange_id("rb2701") == "SHFE"
    assert provider.last_catalog_error == "ctp_catalog_not_built"
    assert [kw["instrument_id"] for kw in calls] == ["", "rb2701"]
    assert provider.catalog_queries == 1  # the single query


def test_provider_reserve_uses_the_catalog_venue(tmp_path):
    provider, trader, _calls, _cache_ = _provider(tmp_path, lambda kw: _result(CATALOG_ROWS))
    trader._next = 40
    trader.next_order_ref = lambda: 41

    class Req:
        symbol = "IF2612"
        exchange_id = None
        order_type = type("T", (), {"value": "limit"})()
        side = type("S", (), {"value": "buy"})()
        price = 4000
        quantity = 1
        position_id = None
        time_in_force = "GFD"
        offset = "open"
        reduce_only = False
        position_side = None
        position_mode = None
        client_order_id = "t1"

    reservation = provider.reserve_order(Req())
    assert dict(reservation.identity.fields)["exchange_id"] == "CFFEX"
