"""离线契约测试：sftp 后端的目录遍历、跳过与递归删除（用内存假传输）。"""

from __future__ import annotations

import stat as stat_module
from pathlib import Path

import pytest

from bt_api_ctp.cleaner.config import HostConfig
from bt_api_ctp.cleaner.pull.backend import PullError
from bt_api_ctp.cleaner.pull.sftp_backend import SftpBackend

_DIR = stat_module.S_IFDIR | 0o755
_FILE = stat_module.S_IFREG | 0o644


class Entry:
    def __init__(self, filename: str, *, is_dir: bool, size: int = 0):
        self.filename = filename
        self.st_mode = _DIR if is_dir else _FILE
        self.st_size = size


class FakeSftp:
    """In-memory remote tree: ``dirs`` maps a path to its entries."""

    def __init__(self, dirs, files):
        self.dirs = dict(dirs)
        self.files = dict(files)
        self.fetched: list[tuple[str, str]] = []
        self.removed: list[str] = []
        self.rmdirs: list[str] = []

    def listdir_attr(self, path):
        if path not in self.dirs:
            raise OSError(f"no such directory: {path}")
        return list(self.dirs[path])

    def stat(self, path):
        if path not in self.files:
            raise OSError(f"no such file: {path}")

        class _Stat:
            st_size = len(self.files[path])

        return _Stat()

    def get(self, remotepath, localpath):
        if remotepath not in self.files:
            raise OSError(remotepath)
        Path(localpath).write_bytes(self.files[remotepath])
        self.fetched.append((remotepath, localpath))

    def remove(self, path):
        self.removed.append(path)
        self.files.pop(path, None)

    def rmdir(self, path):
        self.rmdirs.append(path)
        self.dirs.pop(path, None)


def _remote() -> FakeSftp:
    return FakeSftp(
        dirs={
            "D:/tick": [
                Entry("20260918", is_dir=True),
                Entry("20260917", is_dir=True),
                Entry("notes.txt", is_dir=False),
            ],
            "D:/tick/20260918": [
                Entry("SHFE", is_dir=True),
                Entry(".staging", is_dir=True),
                Entry("report.json", is_dir=False, size=2),
            ],
            "D:/tick/20260918/SHFE": [
                Entry("rb2510.parquet", is_dir=False, size=8),
                Entry("rb2510.parquet.lock", is_dir=False, size=4),
            ],
            "D:/tick/20260918/.staging": [Entry("0000.parquet", is_dir=False, size=99)],
        },
        files={
            "D:/tick/20260918/report.json": b"{}",
            "D:/tick/20260918/SHFE/rb2510.parquet": b"12345678",
            "D:/tick/20260918/SHFE/rb2510.parquet.lock": b"lock",
            "D:/tick/20260918/.staging/0000.parquet": b"segment",
        },
    )


def _backend(sftp: FakeSftp, *, port: int = 22) -> SftpBackend:
    host = HostConfig(
        name="win-b", backend="sftp", host="user@10.0.0.2", remote_data_root="D:/tick", port=port
    )
    return SftpBackend(host, connector=lambda _: sftp)


class TestListing:
    def test_lists_days(self):
        backend = _backend(_remote())

        assert backend.list_days() == ["20260917", "20260918"]

    def test_lists_final_files_and_skips_staging(self):
        backend = _backend(_remote())

        rel_paths = {file.rel_path for file in backend.list_day_files("20260918")}

        assert rel_paths == {
            "20260918/SHFE/rb2510.parquet",
            "20260918/report.json",
        }

    def test_has_report(self):
        backend = _backend(_remote())

        assert backend.has_report("20260918") is True
        assert backend.has_report("20260917") is False

    def test_invalid_day_is_refused(self):
        backend = _backend(_remote())

        with pytest.raises(PullError):
            backend.list_day_files("2026-09-18")


class TestFetch:
    def test_downloads_files_to_the_staging_day(self, tmp_path):
        sftp = _remote()
        backend = _backend(sftp)

        result = backend.fetch_day("20260918", tmp_path / "staging")

        assert result.files == 2
        assert result.skipped == 0
        target = tmp_path / "staging" / "20260918" / "SHFE" / "rb2510.parquet"
        assert target.read_bytes() == b"12345678"
        assert not list(target.parent.glob("*.part"))

    def test_matching_size_is_skipped(self, tmp_path):
        sftp = _remote()
        backend = _backend(sftp)
        target = tmp_path / "staging" / "20260918" / "SHFE" / "rb2510.parquet"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"12345678")

        result = backend.fetch_day("20260918", tmp_path / "staging")

        assert result.skipped == 1
        assert result.files == 1
        assert [remote for remote, _ in sftp.fetched] == ["D:/tick/20260918/report.json"]
        assert (tmp_path / "staging" / "20260918" / "report.json").read_bytes() == b"{}"

    def test_get_failure_leaves_no_partial_file(self, tmp_path):
        sftp = _remote()

        def failing_get(remotepath, localpath):
            Path(localpath).write_bytes(b"half")
            raise OSError("connection reset")

        sftp.get = failing_get
        backend = _backend(sftp)

        with pytest.raises(IOError):
            backend.fetch_day("20260918", tmp_path / "staging")

        assert not list((tmp_path / "staging").rglob("*.part"))


class TestDelete:
    def test_delete_removes_the_tree_recursively(self):
        sftp = _remote()
        backend = _backend(sftp)

        backend.delete_day("20260918")

        assert "D:/tick/20260918/SHFE/rb2510.parquet" in sftp.removed
        assert "D:/tick/20260918/report.json" in sftp.removed
        assert "D:/tick/20260918/.staging/0000.parquet" in sftp.removed
        assert "D:/tick/20260918/SHFE" in sftp.rmdirs
        assert "D:/tick/20260918" in sftp.rmdirs

    def test_delete_refuses_a_path_that_is_not_a_day(self):
        sftp = _remote()
        backend = _backend(sftp)

        with pytest.raises(PullError):
            backend.delete_day("..")

        assert sftp.removed == []


class TestCheck:
    def test_check_reports_ok(self):
        status = _backend(_remote()).check()

        assert status.ok is True

    def test_check_reports_failure_for_a_missing_root(self):
        sftp = FakeSftp(dirs={}, files={})

        status = _backend(sftp).check()

        assert status.ok is False
