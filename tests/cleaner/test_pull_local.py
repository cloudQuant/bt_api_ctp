"""离线契约测试：local 拉取后端（同时是三个后端的语义基准）。"""

from __future__ import annotations

import pytest

from bt_api_ctp.cleaner.config import HostConfig
from bt_api_ctp.cleaner.pull.backend import PullError
from bt_api_ctp.cleaner.pull.local_backend import LocalBackend


def _host(root) -> HostConfig:
    return HostConfig(name="mirror", backend="local", remote_data_root=str(root))


def _remote_tree(tmp_path):
    root = tmp_path / "remote"
    shfe = root / "20260918" / "SHFE"
    shfe.mkdir(parents=True)
    (shfe / "rb2510.parquet").write_bytes(b"rb-data")
    (shfe / "rb2510.parquet.lock").write_bytes(b"lock")
    (shfe / "ag2612.parquet").write_bytes(b"ag-data-longer")
    dce = root / "20260918" / "DCE"
    dce.mkdir(parents=True)
    (dce / "m2509.parquet").write_bytes(b"m-data")
    (root / "20260918" / "report.json").write_text("{}", encoding="utf-8")
    (root / "20260917" / "SHFE").mkdir(parents=True)
    (root / "20260917" / "SHFE" / "rb2510.parquet").write_bytes(b"older")
    (root / "not-a-day").mkdir()
    staging = root / "20260918" / ".staging" / "run1"
    staging.mkdir(parents=True)
    (staging / "00000000.parquet").write_bytes(b"segment")
    return root


class TestListing:
    def test_lists_only_trading_day_directories(self, tmp_path):
        backend = LocalBackend(_host(_remote_tree(tmp_path)))

        assert backend.list_days() == ["20260917", "20260918"]

    def test_lists_final_files_and_excludes_runtime_state(self, tmp_path):
        backend = LocalBackend(_host(_remote_tree(tmp_path)))

        rel_paths = {file.rel_path for file in backend.list_day_files("20260918")}

        assert rel_paths == {
            "20260918/SHFE/rb2510.parquet",
            "20260918/SHFE/ag2612.parquet",
            "20260918/DCE/m2509.parquet",
            "20260918/report.json",
        }

    def test_file_sizes_are_reported(self, tmp_path):
        backend = LocalBackend(_host(_remote_tree(tmp_path)))

        sizes = {file.rel_path: file.size_bytes for file in backend.list_day_files("20260918")}

        assert sizes["20260918/SHFE/rb2510.parquet"] == len(b"rb-data")

    def test_has_report_is_the_completion_signal(self, tmp_path):
        root = _remote_tree(tmp_path)
        backend = LocalBackend(_host(root))

        assert backend.has_report("20260918") is True
        assert backend.has_report("20260917") is False

    def test_missing_day_lists_nothing(self, tmp_path):
        backend = LocalBackend(_host(_remote_tree(tmp_path)))

        assert backend.list_day_files("20990101") == []

    def test_missing_root_is_a_failed_check(self, tmp_path):
        backend = LocalBackend(_host(tmp_path / "nowhere"))

        status = backend.check()

        assert status.ok is False
        assert "not found" in status.detail
        with pytest.raises(PullError):
            backend.list_days()


class TestFetch:
    def test_fetches_every_final_file_with_stats(self, tmp_path):
        root = _remote_tree(tmp_path)
        backend = LocalBackend(_host(root))

        result = backend.fetch_day("20260918", tmp_path / "staging")

        assert result.files == 4
        assert result.skipped == 0
        assert result.bytes > 0
        assert (
            tmp_path / "staging" / "20260918" / "SHFE" / "rb2510.parquet"
        ).read_bytes() == b"rb-data"
        assert (tmp_path / "staging" / "20260918" / "report.json").exists()
        assert not (tmp_path / "staging" / "20260918" / "SHFE" / "rb2510.parquet.lock").exists()

    def test_second_fetch_skips_complete_files(self, tmp_path):
        root = _remote_tree(tmp_path)
        backend = LocalBackend(_host(root))
        backend.fetch_day("20260918", tmp_path / "staging")

        result = backend.fetch_day("20260918", tmp_path / "staging")

        assert result.files == 0
        assert result.skipped == 4
        assert result.bytes == 0

    def test_a_file_resized_after_the_first_copy_is_fetched_again(self, tmp_path):
        root = _remote_tree(tmp_path)
        backend = LocalBackend(_host(root))
        backend.fetch_day("20260918", tmp_path / "staging")
        target = tmp_path / "staging" / "20260918" / "SHFE" / "rb2510.parquet"
        target.write_bytes(b"truncated")

        result = backend.fetch_day("20260918", tmp_path / "staging")

        assert result.skipped == 3
        assert target.read_bytes() == b"rb-data"

    def test_fetching_a_missing_day_raises(self, tmp_path):
        backend = LocalBackend(_host(_remote_tree(tmp_path)))

        with pytest.raises(PullError):
            backend.fetch_day("20990101", tmp_path / "staging")


class TestDelete:
    def test_delete_removes_the_day(self, tmp_path):
        root = _remote_tree(tmp_path)
        backend = LocalBackend(_host(root))

        backend.delete_day("20260918")

        assert not (root / "20260918").exists()
        assert (root / "20260917").exists()

    def test_delete_of_a_missing_day_is_a_noop(self, tmp_path):
        backend = LocalBackend(_host(_remote_tree(tmp_path)))

        backend.delete_day("20990101")
