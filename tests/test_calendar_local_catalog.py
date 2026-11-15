"""Offline tests of the explicit local-only CTP catalog reader."""

from __future__ import annotations

import json
import multiprocessing
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from bt_api_ctp import calendar_local_catalog as local
from bt_api_ctp.instrument_catalog_cache import (
    InstrumentCatalogCache,
    InstrumentCatalogEntry,
    _catalog_from_entries,
    _content_digest,
    _encode_catalog,
)

SCOPE = "a" * 64
DAY = "20261106"
BUILT = "2026-11-06T01:00:00Z"


def entry(instrument, venue="SHFE", **kwargs):
    values = {
        "instrument_id": instrument,
        "exchange_id": venue,
        "product_id": "rb",
        "product_class": "1",
        "open_date": "20260101",
        "expire_date": "20270115",
    }
    values.update(kwargs)
    return InstrumentCatalogEntry(**values)


def encoded(entries=None, *, conflicts=(), skipped=0, scope=SCOPE, day=DAY):
    if entries is None:
        entries = [
            entry("rb2701"),
            entry("rb2701-C-3000", product_class="2", underlying_instrument_id="rb2701"),
        ]
    return _encode_catalog(
        _catalog_from_entries(
            scope, day, entries, conflicts=conflicts, skipped_rows=skipped, built_at_utc=BUILT
        )
    )


@pytest.fixture
def cache_root(tmp_path):
    root = tmp_path.resolve() / "cache"
    (root / SCOPE).mkdir(parents=True)
    (root / SCOPE / f"{DAY}.json").write_bytes(encoded())
    return root


def read(root, *, scope=SCOPE, day=DAY, filesystem=local.TRUSTED_LOCAL_POSIX):
    return local.read_local_catalog(
        cache_dir=root, scope=scope, trading_day=day, filesystem=filesystem
    )


def files(root):
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file() and not path.is_symlink()
    }


def test_reads_actual_format_without_query_or_writes(cache_root, monkeypatch):
    before = files(cache_root)

    def forbidden(*args, **kwargs):
        raise AssertionError("native lookup or network was called")

    monkeypatch.setattr(InstrumentCatalogCache, "lookup", forbidden)
    monkeypatch.setattr(InstrumentCatalogCache, "_supplement", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    snapshot = read(cache_root)
    assert (snapshot.status, snapshot.reason) == ("CANDIDATE", "UNVERIFIED_CACHE")
    assert [item.kind for item in snapshot.entries] == ["future", "option"]
    assert all(item.identity_status == "UNVERIFIED_DECLARATION" for item in snapshot.entries)
    assert all(item.lifecycle_status == "WITHIN_DECLARED_BOUNDS" for item in snapshot.entries)
    assert [item.underlying_relation for item in snapshot.entries] == [
        "NOT_APPLICABLE",
        "NOT_PROVEN",
    ]
    assert snapshot.built_at_utc == BUILT
    assert snapshot.metadata_digest_protection == "NOT_COVERED"
    assert snapshot.completeness == "UNVERIFIED_DECLARATION"
    assert (snapshot.source_authentication, snapshot.freshness, snapshot.absence) == (
        "NOT_PROVEN",
        "NOT_PROVEN",
        "NOT_PROVEN",
    )
    assert snapshot.writer_quiescence == "NOT_PROVEN"
    assert snapshot.publication == "NOT_AUTHORIZED"
    assert SCOPE not in repr(snapshot) and "rb2701" not in repr(snapshot)
    assert files(cache_root) == before


def test_unknown_product_and_incomplete_metadata_are_preserved_as_partial(cache_root):
    source = [
        entry("known"),
        entry("unknown", product_class=None, product_id=None, open_date=None),
        entry("unsupported", product_class="3"),
        entry("option-missing-underlying", product_class="2"),
        entry("bad id", product_id="rb product"),
        entry("other", venue="OTHER"),
        entry("duplicate", "SHFE"),
        entry("duplicate", "DCE"),
        entry("bad-date", open_date="00000000", expire_date="20260101"),
    ]
    (cache_root / SCOPE / f"{DAY}.json").write_bytes(
        encoded(source, conflicts=("conflict",), skipped=2)
    )
    snapshot = read(cache_root)
    assert (snapshot.status, snapshot.reason) == ("PARTIAL", "REFERENCE_GAPS_PRESENT")
    assert snapshot.skipped_rows == 2 and snapshot.conflicts == ("conflict",)
    by_name = {}
    for item in snapshot.entries:
        by_name.setdefault(item.entry.instrument_id, []).append(item)
    assert by_name["unknown"][0].kind == "UNKNOWN"
    assert set(by_name["unknown"][0].gaps) == {
        "PRODUCT_CLASS_UNKNOWN",
        "PRODUCT_ID_UNKNOWN",
        "LIFECYCLE_DATES_UNKNOWN",
    }
    assert by_name["unsupported"][0].kind == "UNKNOWN"
    assert by_name["unsupported"][0].underlying_relation == "UNKNOWN"
    assert "UNDERLYING_UNKNOWN" in by_name["option-missing-underlying"][0].gaps
    assert "INSTRUMENT_ID_UNKNOWN" in by_name["bad id"][0].gaps
    assert "PRODUCT_ID_UNKNOWN" in by_name["bad id"][0].gaps
    assert "VENUE_UNKNOWN" in by_name["other"][0].gaps
    assert all("INSTRUMENT_ID_AMBIGUOUS" in item.gaps for item in by_name["duplicate"])
    assert by_name["bad-date"][0].lifecycle_status == "UNKNOWN"


@pytest.mark.parametrize(
    "opened,expires,expected,overall",
    [
        ("20261106", "20261106", "WITHIN_DECLARED_BOUNDS", "CANDIDATE"),
        ("20261106", "20261201", "WITHIN_DECLARED_BOUNDS", "CANDIDATE"),
        ("20260101", "20261106", "WITHIN_DECLARED_BOUNDS", "CANDIDATE"),
        ("20300101", "20310101", "OUTSIDE_DECLARED_BOUNDS", "PARTIAL"),
        ("20250101", "20251101", "OUTSIDE_DECLARED_BOUNDS", "PARTIAL"),
        (None, "20270101", "UNKNOWN", "PARTIAL"),
        ("20270101", "20260101", "UNKNOWN", "PARTIAL"),
        ("20260230", "20270101", "UNKNOWN", "PARTIAL"),
    ],
)
def test_lifecycle_bounds_are_unverified_declarations(
    cache_root, opened, expires, expected, overall
):
    instrument = entry("rb2701", open_date=opened, expire_date=expires)
    (cache_root / SCOPE / f"{DAY}.json").write_bytes(encoded([instrument]))
    result = read(cache_root)
    assert result.status == overall
    assert result.entries[0].entry == instrument
    assert result.entries[0].lifecycle_status == expected
    assert result.source_authentication == result.freshness == "NOT_PROVEN"
    assert ("OUTSIDE_DECLARED_LIFECYCLE" in result.entries[0].gaps) == (
        expected == "OUTSIDE_DECLARED_BOUNDS"
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("instrument_id", "rb/../x"),
        ("instrument_id", "_rb"),
        ("instrument_id", "合约"),
        ("product_id", "rb/product"),
        ("underlying_instrument_id", "rb@2701"),
    ],
)
def test_identity_must_match_downstream_calendar_contract(cache_root, field, value):
    kwargs = {"product_class": "2", "underlying_instrument_id": "rb2701", field: value}
    instrument = entry(**({"instrument": "rb2701-C-3000"} | kwargs))
    (cache_root / SCOPE / f"{DAY}.json").write_bytes(encoded([instrument]))
    result = read(cache_root)
    assert result.status == "PARTIAL"
    assert result.entries[0].entry == instrument
    assert result.entries[0].identity_status == "UNKNOWN"
    assert result.entries[0].kind == "option"


def test_calendar_identity_length_and_hyphen_compatibility():
    assert local._identity_text("rb2701-C-3000")
    assert local._identity_text("A" * 128)
    assert not local._identity_text("A" * 129)


def test_option_without_cached_underlying_retains_unproven_relation(cache_root):
    option = entry("rb2701-C-3000", product_class="2", underlying_instrument_id="rb2701")
    (cache_root / SCOPE / f"{DAY}.json").write_bytes(encoded([option]))
    result = read(cache_root)
    assert result.status == "CANDIDATE"
    assert len(result.entries) == 1 and result.entries[0].entry == option
    assert result.entries[0].underlying_relation == "NOT_PROVEN"
    assert result.absence == "NOT_PROVEN"


def test_unhashed_cache_metadata_remains_unverified_and_is_not_invented(cache_root):
    path = cache_root / SCOPE / f"{DAY}.json"
    document = json.loads(path.read_bytes())
    original_digest = document["content_sha256"]
    document["built_at_utc"] = "2025-01-01T00:00:00Z"
    document["published_at_utc"] = "2025-01-02T00:00:00Z"
    document["skipped_rows"] = 4
    path.write_text(json.dumps(document), encoding="utf-8")
    result = read(cache_root)
    assert result.status == "PARTIAL"
    assert result.built_at_utc == "2025-01-01T00:00:00Z"
    assert result.published_at_utc == "2025-01-02T00:00:00Z"
    assert result.skipped_rows == 4 and result.content_sha256 == original_digest
    assert result.metadata_digest_protection == "NOT_COVERED"
    for invalid in (None, "", "invalid", "2026-02-30T01:00:00Z"):
        document["built_at_utc"] = invalid
        path.write_text(json.dumps(document), encoding="utf-8")
        assert read(cache_root).reason == "CACHE_INVALID"
    del document["built_at_utc"]
    path.write_text(json.dumps(document), encoding="utf-8")
    assert read(cache_root).reason == "CACHE_INVALID"


@pytest.mark.parametrize(
    "damage",
    [
        "digest",
        "wrong_scope",
        "wrong_day",
        "unknown_key",
        "duplicate",
        "truncated",
        "empty",
        "duplicate_conflict",
    ],
)
def test_invalid_content_is_never_a_complete_result(cache_root, damage):
    path = cache_root / SCOPE / f"{DAY}.json"
    document = json.loads(path.read_bytes())
    if damage == "digest":
        document["content_sha256"] = "0" * 64
    elif damage == "wrong_scope":
        document["scope"] = "b" * 64
    elif damage == "wrong_day":
        document["trading_day"] = "20261109"
    elif damage == "unknown_key":
        document["unexpected"] = 1
    elif damage == "duplicate_conflict":
        document["conflicts"] = ["x", "x"]
    elif damage == "empty":
        path.write_bytes(encoded([]))
        assert read(cache_root).reason == "EMPTY_CATALOG"
        return
    elif damage == "truncated":
        path.write_bytes(b"{")
        assert read(cache_root).reason == "CACHE_INVALID"
        return
    if damage == "duplicate":
        raw = path.read_text(encoding="utf-8")
        path.write_text(raw[:-1] + ',"format":"bt_api_ctp.instrument_catalog"}', encoding="utf-8")
    else:
        path.write_text(json.dumps(document), encoding="utf-8")
    assert (read(cache_root).status, read(cache_root).reason) == ("UNKNOWN", "CACHE_INVALID")


@pytest.mark.parametrize(
    "scope,day",
    [
        ("b" * 64, DAY),
        (SCOPE, "20261109"),
        ("bad", DAY),
        (SCOPE, "20260230"),
        (SCOPE, "2026-11-06"),
    ],
)
def test_explicit_scope_day_are_required_without_any_fallback(cache_root, scope, day):
    before = files(cache_root)
    assert read(cache_root, scope=scope, day=day).status == "UNKNOWN"
    assert files(cache_root) == before


def test_missing_unsupported_relative_and_oversized_are_unknown(cache_root, tmp_path, monkeypatch):
    assert read(tmp_path / "absent").status == "UNKNOWN"
    assert read(Path("relative")).status == "UNKNOWN"
    assert read(cache_root, filesystem="").reason == "FILESYSTEM_UNSUPPORTED"
    monkeypatch.setattr(local, "MAX_FILE_BYTES", 32)
    result = read(cache_root)
    assert (result.status, result.reason) == ("UNKNOWN", "CACHE_RESOURCE_LIMIT")
    assert not (tmp_path / "absent").exists()


def test_node_budget_and_nested_entry_values_fail_closed(cache_root, monkeypatch):
    monkeypatch.setattr(local, "MAX_JSON_NODES", 10)
    assert (read(cache_root).status, read(cache_root).reason) == ("UNKNOWN", "CACHE_RESOURCE_LIMIT")
    monkeypatch.setattr(local, "MAX_JSON_NODES", 3_000_000)
    path = cache_root / SCOPE / f"{DAY}.json"
    document = json.loads(path.read_bytes())
    document["entries"][0]["product_id"] = ["not", "a", "native", "field"]
    path.write_text(json.dumps(document), encoding="utf-8")
    assert (read(cache_root).status, read(cache_root).reason) == ("UNKNOWN", "CACHE_INVALID")


def test_entry_and_conflict_limits_have_finite_resource_reason(cache_root, monkeypatch):
    monkeypatch.setattr(local, "MAX_ENTRIES", 1)
    assert read(cache_root).reason == "CACHE_RESOURCE_LIMIT"
    monkeypatch.setattr(local, "MAX_ENTRIES", 100_000)
    path = cache_root / SCOPE / f"{DAY}.json"
    path.write_bytes(encoded(conflicts=("a", "b")))
    monkeypatch.setattr(local, "MAX_CONFLICTS", 1)
    assert read(cache_root).reason == "CACHE_RESOURCE_LIMIT"


@pytest.mark.parametrize("damage", ["duplicate_entry", "conflicting_entry"])
def test_self_consistent_digest_does_not_hide_identity_conflicts(cache_root, damage):
    path = cache_root / SCOPE / f"{DAY}.json"
    document = json.loads(path.read_bytes())
    if damage == "duplicate_entry":
        document["entries"].append(dict(document["entries"][0]))
        document["entry_count"] += 1
    else:
        document["conflicts"] = [document["entries"][0]["instrument_id"]]
    document["content_sha256"] = _content_digest(
        SCOPE, DAY, document["conflicts"], document["entries"]
    )
    path.write_text(json.dumps(document), encoding="utf-8")
    assert (read(cache_root).status, read(cache_root).reason) == ("UNKNOWN", "CACHE_INVALID")


def test_rejects_symlink_in_any_component_or_target(cache_root, tmp_path):
    link_root = tmp_path.resolve() / "linked"
    link_root.symlink_to(cache_root, target_is_directory=True)
    assert read(link_root).status == "UNKNOWN"
    target = cache_root / SCOPE / f"{DAY}.json"
    other = cache_root / SCOPE / "other"
    target.rename(other)
    target.symlink_to(other)
    assert read(cache_root).status == "UNKNOWN"
    target.unlink()
    scope = cache_root / SCOPE
    scope.rename(cache_root / "real-scope")
    scope.symlink_to(cache_root / "real-scope", target_is_directory=True)
    assert read(cache_root).status == "UNKNOWN"


def test_non_regular_file_is_not_read_or_waited_on(cache_root):
    path = cache_root / SCOPE / f"{DAY}.json"
    path.unlink()
    os.mkfifo(path)
    assert (read(cache_root).status, read(cache_root).reason) == (
        "UNKNOWN",
        "CACHE_UNAVAILABLE_OR_CHANGED",
    )


def test_replaced_file_during_read_is_not_accepted(cache_root, monkeypatch):
    path = cache_root / SCOPE / f"{DAY}.json"
    replacement = path.with_suffix(".new")
    replacement.write_bytes(encoded([entry("other")]))
    original = local.os.read
    changed = False

    def racing_read(fd, size):
        nonlocal changed
        chunk = original(fd, size)
        if not changed and chunk:
            os.replace(replacement, path)
            changed = True
        return chunk

    monkeypatch.setattr(local.os, "read", racing_read)
    assert read(cache_root).status == "UNKNOWN"
    assert changed


def test_local_subprocess_reads_cache_without_socket_or_lookup(cache_root):
    script = """
import socket, sys
def audit(event, args):
    if event in {'socket.connect', 'socket.connect_ex', 'socket.getaddrinfo'}:
        raise AssertionError('network called during import or read')
sys.addaudithook(audit)
from bt_api_ctp import calendar_local_catalog as local
from bt_api_ctp.instrument_catalog_cache import InstrumentCatalogCache
def forbidden(*args, **kwargs): raise AssertionError('native or network called')
socket.socket = forbidden
InstrumentCatalogCache.lookup = forbidden
result = local.read_local_catalog(cache_dir=sys.argv[1], scope=sys.argv[2], trading_day=sys.argv[3], filesystem=local.TRUSTED_LOCAL_POSIX)
assert result.status == 'CANDIDATE'
assert [item.kind for item in result.entries] == ['future', 'option']
print('PASS')
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(cache_root), SCOPE, DAY],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "PASS"


def _reader_process(root, start, output):
    start.wait()
    for _ in range(20):
        result = read(root)
        output.put((result.status, tuple(item.entry.instrument_id for item in result.entries)))


def test_atomic_concurrent_replacements_never_create_partial_rows(cache_root):
    # Every writer output is a complete valid file; a racing reader may fail
    # identity checks, but cannot present a mixed row set as a candidate.
    path = cache_root / SCOPE / f"{DAY}.json"
    second = encoded([entry("fresh")])
    first = encoded([entry("prior")])
    path.write_bytes(first)
    ctx = multiprocessing.get_context("spawn")
    start, output = ctx.Event(), ctx.Queue()
    process = ctx.Process(target=_reader_process, args=(cache_root, start, output))
    process.start()
    start.set()
    for index in range(20):
        temporary = path.with_suffix(".new")
        temporary.write_bytes(second if index % 2 else first)
        os.replace(temporary, path)
    results = [output.get(timeout=20) for _ in range(20)]
    process.join(20)
    assert process.exitcode == 0
    assert all(
        status == "UNKNOWN"
        or (status, names) in {("CANDIDATE", ("prior",)), ("CANDIDATE", ("fresh",))}
        for status, names in results
    )
