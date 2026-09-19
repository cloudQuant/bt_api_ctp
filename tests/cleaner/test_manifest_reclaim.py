"""离线契约测试：manifest 状态机持久化与安全删除决策。"""

from __future__ import annotations

import json

import pytest

from bt_api_ctp.cleaner.config import HostConfig
from bt_api_ctp.cleaner.manifest import (
    STATUS_DELETED,
    STATUS_FAILED,
    STATUS_MERGED,
    STATUS_PULLED,
    STATUS_VERIFIED,
    Manifest,
)
from bt_api_ctp.cleaner.pull.local_backend import LocalBackend
from bt_api_ctp.cleaner.reclaim import (
    execute_reclaim,
    plan_reclaim,
    safe_day_path,
)


class TestManifest:
    def test_marks_are_persisted_with_timestamps(self, tmp_path):
        manifest = Manifest(tmp_path / "manifest.json")

        manifest.mark("a", "20260918", STATUS_PULLED)
        manifest.mark("a", "20260918", STATUS_VERIFIED, files=[{"rel_path": "x", "rows": 1}])

        reloaded = Manifest(tmp_path / "manifest.json")
        entry = reloaded.entry("a", "20260918")
        assert entry.status == STATUS_VERIFIED
        assert entry.pulled_at is not None
        assert entry.verified_at is not None
        assert entry.files == [{"rel_path": "x", "rows": 1}]

    def test_status_and_days_accessors(self, tmp_path):
        manifest = Manifest(tmp_path / "manifest.json")
        manifest.mark("a", "20260918", STATUS_PULLED)
        manifest.mark("a", "20260917", STATUS_MERGED)
        manifest.mark("b", "20260918", STATUS_PULLED)

        assert manifest.status("a", "20260918") == STATUS_PULLED
        assert manifest.status("a", "20990101") is None
        assert manifest.days("a") == ["20260917", "20260918"]

    def test_failure_records_the_error_and_success_clears_it(self, tmp_path):
        manifest = Manifest(tmp_path / "manifest.json")

        manifest.mark("a", "20260918", STATUS_FAILED, error="size mismatch")
        assert manifest.entry("a", "20260918").error == "size mismatch"

        manifest.mark("a", "20260918", STATUS_VERIFIED)
        assert manifest.entry("a", "20260918").error is None

    def test_file_is_valid_json_after_every_mark(self, tmp_path):
        path = tmp_path / "manifest.json"
        manifest = Manifest(path)

        manifest.mark("a", "20260918", STATUS_MERGED)

        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["version"] == 1
        assert payload["hosts"]["a"]["20260918"]["status"] == STATUS_MERGED

    def test_no_temporary_file_is_left_behind(self, tmp_path):
        manifest = Manifest(tmp_path / "manifest.json")

        manifest.mark("a", "20260918", STATUS_MERGED)

        assert list(tmp_path.glob("*.tmp")) == []


class TestPlanReclaim:
    def _manifest_with(self, tmp_path, status):
        manifest = Manifest(tmp_path / "manifest.json")
        manifest.mark("a", "20260918", status)
        return manifest

    def test_merged_day_may_be_deleted(self, tmp_path):
        decision = plan_reclaim(
            self._manifest_with(tmp_path, STATUS_MERGED), "a", "20260918", enabled=True
        )

        assert decision.delete is True
        assert "verified and merged" in decision.reason

    @pytest.mark.parametrize(
        "status", [STATUS_PULLED, STATUS_VERIFIED, STATUS_FAILED, STATUS_DELETED]
    )
    def test_non_merged_states_are_never_deleted(self, tmp_path, status):
        decision = plan_reclaim(
            self._manifest_with(tmp_path, status), "a", "20260918", enabled=True
        )

        assert decision.delete is False

    def test_unknown_day_is_never_deleted(self, tmp_path):
        decision = plan_reclaim(Manifest(tmp_path / "manifest.json"), "a", "20260918", enabled=True)

        assert decision.delete is False
        assert decision.reason == "not in manifest"

    def test_disabled_config_blocks_even_a_merged_day(self, tmp_path):
        decision = plan_reclaim(
            self._manifest_with(tmp_path, STATUS_MERGED), "a", "20260918", enabled=False
        )

        assert decision.delete is False
        assert "disabled" in decision.reason


class TestSafeDayPath:
    def test_builds_the_day_path(self):
        assert safe_day_path("/data/tick", "20260918") == "/data/tick/20260918"

    def test_trailing_separator_is_normalised(self):
        assert safe_day_path("D:/tick/", "20260918") == "D:/tick/20260918"

    @pytest.mark.parametrize("day", ["..", "2026-09-18", "", "20260918/../etc"])
    def test_malformed_days_are_refused(self, day):
        with pytest.raises(ValueError):
            safe_day_path("/data/tick", day)

    def test_empty_root_is_refused(self):
        with pytest.raises(ValueError):
            safe_day_path("", "20260918")


class _FakeBackend:
    backend = "fake"

    def __init__(self, name: str, root: str, *, fail: bool = False):
        self.name = name
        self.remote_root = root
        self.deleted: list[str] = []
        self._fail = fail

    def delete_day(self, day: str) -> None:
        if self._fail:
            raise RuntimeError("network down")
        self.deleted.append(day)

    def close(self) -> None: ...


class TestExecuteReclaim:
    def _manifest(self, tmp_path, status=STATUS_MERGED):
        manifest = Manifest(tmp_path / "manifest.json")
        manifest.mark("a", "20260918", status)
        return manifest

    def test_deletes_and_marks_deleted(self, tmp_path):
        manifest = self._manifest(tmp_path)
        backend = _FakeBackend("a", "/data/tick")

        report = execute_reclaim(manifest, backend)

        assert backend.deleted == ["20260918"]
        assert report.deleted == ["a/20260918"]
        assert manifest.status("a", "20260918") == STATUS_DELETED

    def test_unmerged_day_is_not_touched(self, tmp_path):
        manifest = self._manifest(tmp_path, STATUS_VERIFIED)
        backend = _FakeBackend("a", "/data/tick")

        report = execute_reclaim(manifest, backend)

        assert backend.deleted == []
        assert report.planned == []
        assert manifest.status("a", "20260918") == STATUS_VERIFIED

    def test_dry_run_plans_without_deleting(self, tmp_path):
        manifest = self._manifest(tmp_path)
        backend = _FakeBackend("a", "/data/tick")

        report = execute_reclaim(manifest, backend, dry_run=True)

        assert report.planned == ["a/20260918"]
        assert backend.deleted == []
        assert report.decisions[0].path == "/data/tick/20260918"
        assert manifest.status("a", "20260918") == STATUS_MERGED

    def test_disabled_config_prevents_deletion(self, tmp_path):
        manifest = self._manifest(tmp_path)
        backend = _FakeBackend("a", "/data/tick")

        report = execute_reclaim(manifest, backend, enabled=False)

        assert backend.deleted == []
        assert report.planned == []
        assert manifest.status("a", "20260918") == STATUS_MERGED

    def test_delete_failure_stays_merged_for_a_retry(self, tmp_path):
        manifest = self._manifest(tmp_path)
        backend = _FakeBackend("a", "/data/tick", fail=True)

        report = execute_reclaim(manifest, backend)

        assert report.failed and "network down" in report.failed[0]
        assert manifest.status("a", "20260918") == STATUS_MERGED


class TestExecuteReclaimWithLocalBackend:
    def test_real_directory_is_removed_only_after_merged(self, tmp_path):
        remote = tmp_path / "remote"
        (remote / "20260918" / "SHFE").mkdir(parents=True)
        (remote / "20260918" / "SHFE" / "rb2510.parquet").write_bytes(b"x")
        manifest = Manifest(tmp_path / "manifest.json")
        manifest.mark("mirror", "20260918", STATUS_VERIFIED)
        host = HostConfig(name="mirror", backend="local", remote_data_root=str(remote))
        backend = LocalBackend(host)

        execute_reclaim(manifest, backend)
        assert (remote / "20260918").exists()

        manifest.mark("mirror", "20260918", STATUS_MERGED)
        execute_reclaim(manifest, backend)
        assert not (remote / "20260918").exists()
