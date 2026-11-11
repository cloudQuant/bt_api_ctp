"""Temporary synthetic files only; no real configuration or account data."""

import os
from pathlib import Path

import pytest

from bt_api_ctp import calendar_supplement_reader as reader
from bt_api_ctp.calendar_supplement import MAX_INPUT_BYTES, SupplementError

VALID = """sources:
  - id: sample
    description: 合成公告，不是交易所事实
trading_days:
  - venue: SHFE
    day: '2026-11-12'
    status: open
    source_ids: [sample]
trading_hours: []
exceptions: []
""".encode()


@pytest.fixture
def safe_dir(tmp_path):
    # Resolve platform temp-directory symlinks only in the test fixture.
    return tmp_path.resolve()


@pytest.fixture(autouse=True)
def require_supported():
    if not reader._supported():
        pytest.skip("descriptor-relative no-follow filesystem APIs unavailable")


def test_valid_candidate_preserves_data_and_remains_unverified(safe_dir):
    path = safe_dir / "补充 数据.yaml"
    path.write_bytes(VALID)
    result = reader.read_supplement(path)
    assert result.status == "CANDIDATE"
    assert result.verification == result.candidate.verification == "UNVERIFIED"
    assert result.candidate.content["sources"][0]["description"] == "合成公告，不是交易所事实"
    assert result.candidate.content["trading_days"][0]["source_ids"] == ["sample"]


def test_valid_but_unfinished_intermediate_edit_cannot_be_proven_final(safe_dir):
    path = safe_dir / "file"
    # A writer paused after an intermediate complete, syntactically valid write.
    path.write_bytes(VALID.replace(b"status: open", b"status: closed"))
    result = reader.read_supplement(path)
    assert result.status == "CANDIDATE"
    assert result.candidate.content["trading_days"][0]["status"] == "closed"
    assert result.verification == "UNVERIFIED"


def test_missing_directory_nonregular_and_symlinks_are_distinct(safe_dir):
    assert reader.read_supplement(safe_dir / "missing").status == "MISSING"
    assert reader.read_supplement(safe_dir).status == "NOT_REGULAR"
    target = safe_dir / "target"
    target.write_bytes(VALID)
    link = safe_dir / "link"
    link.symlink_to(target)
    assert reader.read_supplement(link).status == "SYMLINK"
    directory = safe_dir / "actual-dir"
    directory.mkdir()
    (directory / "file").write_bytes(VALID)
    directory_link = safe_dir / "directory-link"
    directory_link.symlink_to(directory, target_is_directory=True)
    assert reader.read_supplement(directory_link / "file").status == "SYMLINK"
    if hasattr(os, "mkfifo"):
        fifo = safe_dir / "fifo"
        os.mkfifo(fifo)
        assert reader.read_supplement(fifo).status == "NOT_REGULAR"


@pytest.mark.parametrize(
    "data,status",
    [
        (b"\xff", "INVALID_UTF8"),
        (b"sources: [", "INVALID_CONTENT"),
        (b"", "INVALID_CONTENT"),
        (b"x" * (MAX_INPUT_BYTES + 1), "TOO_LARGE"),
    ],
)
def test_invalid_content_is_not_silently_empty_or_success(safe_dir, data, status):
    path = safe_dir / "sensitive-name.yaml"
    path.write_bytes(data)
    result = reader.read_supplement(path)
    assert result.status == status
    assert result.candidate is None and result.verification == "UNVERIFIED"
    assert "sensitive-name" not in repr(result)


def test_parser_dependency_unavailable_does_not_become_success(safe_dir, monkeypatch):
    path = safe_dir / "file"
    path.write_bytes(VALID)

    def unavailable(_data):
        raise SupplementError("optional PyYAML dependency unavailable")

    monkeypatch.setattr(reader, "parse_supplement", unavailable)
    assert reader.read_supplement(path).status == "DEPENDENCY_UNAVAILABLE"


def test_unexpected_parser_error_does_not_leak_content(safe_dir, monkeypatch):
    path = safe_dir / "private.yaml"
    path.write_bytes(VALID)

    def broken(_data):
        raise RuntimeError("private raw content")

    monkeypatch.setattr(reader, "parse_supplement", broken)
    result = reader.read_supplement(path)
    assert result.status == "PARSER_ERROR"
    assert "private" not in repr(result)


def test_unsupported_platform_rejected_before_open(safe_dir, monkeypatch):
    monkeypatch.setattr(reader, "_supported", lambda: False)
    assert reader.read_supplement(safe_dir / "file").status == "UNSUPPORTED"


def test_bad_paths_are_rejected_without_leaking_contents():
    for path in (None, "", b"bytes-path", "/tmp/../bad", "abc\x00private"):
        result = reader.read_supplement(path)
        assert result.status == "INVALID_PATH"
        assert "private" not in repr(result)


def read_with_change(path, monkeypatch, action):
    original_read = os.read
    changed = []

    def wrapped(fd, size):
        result = original_read(fd, size)
        if not changed:
            changed.append(True)
            action()
        return result

    monkeypatch.setattr(reader.os, "read", wrapped)
    return reader.read_supplement(path)


def test_truncate_during_read_is_detected(safe_dir, monkeypatch):
    path = safe_dir / "file"
    path.write_bytes(VALID)
    result = read_with_change(path, monkeypatch, lambda: path.write_bytes(b""))
    assert result.status == "CHANGED" and result.candidate is None


def test_same_size_edit_with_restored_mtime_is_detected_by_ctime(safe_dir, monkeypatch):
    path = safe_dir / "file"
    path.write_bytes(VALID)
    initial = path.stat()

    def edit():
        path.write_bytes(VALID.replace(b"status: open", b"status: shut"))
        os.utime(path, ns=(initial.st_atime_ns, initial.st_mtime_ns))

    result = read_with_change(path, monkeypatch, edit)
    assert result.status == "CHANGED" and result.candidate is None


def test_atomic_file_replace_during_read_is_detected(safe_dir, monkeypatch):
    path = safe_dir / "file"
    replacement = safe_dir / "replacement"
    path.write_bytes(VALID)
    replacement.write_bytes(VALID)
    result = read_with_change(path, monkeypatch, lambda: os.replace(replacement, path))
    assert result.status == "CHANGED" and result.candidate is None


def test_parent_directory_replacement_during_read_is_detected(safe_dir, monkeypatch):
    directory = safe_dir / "subdir"
    directory.mkdir()
    path = directory / "file"
    path.write_bytes(VALID)

    def replace_directory():
        directory.rename(safe_dir / "old-dir")
        directory.mkdir()
        (directory / "file").write_bytes(VALID)

    result = read_with_change(path, monkeypatch, replace_directory)
    assert result.status == "CHANGED" and result.candidate is None


def test_absolute_path_ignores_cwd_and_relative_path_is_rejected(safe_dir, monkeypatch):
    path = safe_dir / "file"
    path.write_bytes(VALID)
    assert reader.read_supplement(path).status == "CANDIDATE"
    other = safe_dir / "other"
    other.mkdir()
    monkeypatch.chdir(other)
    assert reader.read_supplement(path).status == "CANDIDATE"
    assert reader.read_supplement(Path("file")).status == "INVALID_PATH"
