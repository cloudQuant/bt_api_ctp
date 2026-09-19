"""离线契约测试：rsync 后端的命令构造与输出解析（不连接真实主机）。"""

from __future__ import annotations

from subprocess import CompletedProcess

import pytest

from bt_api_ctp.cleaner.config import HostConfig
from bt_api_ctp.cleaner.pull.backend import PullError
from bt_api_ctp.cleaner.pull.rsync_backend import RsyncBackend


class FakeRunner:
    """Record every command and answer ssh queries from a handler."""

    def __init__(self, handler=None, *, returncode=0, stderr=""):
        self.calls: list[list[str]] = []
        self._handler = handler
        self._returncode = returncode
        self._stderr = stderr

    def __call__(self, args):
        args = list(args)
        self.calls.append(args)
        if self._handler is not None:
            code, stdout, stderr = self._handler(args)
            return CompletedProcess(args, code, stdout, stderr)
        return CompletedProcess(args, self._returncode, "", self._stderr)

    @property
    def rsync_calls(self):
        return [call for call in self.calls if call[0] == "rsync"]

    @property
    def ssh_calls(self):
        return [call for call in self.calls if call[0] == "ssh"]


def _host(*, port=22) -> HostConfig:
    return HostConfig(
        name="linux-a",
        backend="rsync",
        host="user@10.0.0.1",
        remote_data_root="/data/tick",
        port=port,
    )


class TestListing:
    def test_lists_days_and_ignores_non_day_directories(self):
        runner = FakeRunner(lambda args: (0, "20260918\n20260917\nnot-a-day\n", ""))
        backend = RsyncBackend(_host(), runner)

        assert backend.list_days() == ["20260917", "20260918"]

    def test_list_day_files_parses_relative_paths_and_sizes(self):
        stdout = (
            "20260918/SHFE/rb2510.parquet\t123\n"
            "20260918/report.json\t2\n"
            "20260917/SHFE/old.parquet\t9\n"
            "20260918/SHFE/junk.txt\t1\n"
        )
        runner = FakeRunner(lambda args: (0, stdout, ""))
        backend = RsyncBackend(_host(), runner)

        files = backend.list_day_files("20260918")

        assert [(file.rel_path, file.size_bytes) for file in files] == [
            ("20260918/SHFE/rb2510.parquet", 123),
            ("20260918/report.json", 2),
        ]

    def test_invalid_day_is_refused(self):
        backend = RsyncBackend(_host(), FakeRunner())

        with pytest.raises(PullError):
            backend.list_day_files("../etc")

    def test_has_report_uses_a_remote_test(self):
        runner = FakeRunner(lambda args: (0, "yes\n", ""))
        backend = RsyncBackend(_host(), runner)

        assert backend.has_report("20260918") is True
        assert "report.json" in runner.ssh_calls[-1][-1]

    def test_ssh_failure_raises_with_stderr(self):
        runner = FakeRunner(lambda args: (255, "", "permission denied"))
        backend = RsyncBackend(_host(), runner)

        with pytest.raises(PullError) as error:
            backend.list_days()

        assert "permission denied" in str(error.value)


class TestFetch:
    def test_rsync_command_excludes_runtime_files_and_uses_the_port(self, tmp_path):
        listing = "20260918/SHFE/rb2510.parquet\t8\n20260918/report.json\t2\n"
        runner = FakeRunner(lambda args: (0, listing, ""))
        backend = RsyncBackend(_host(port=2222), runner)

        result = backend.fetch_day("20260918", tmp_path / "staging")

        call = runner.rsync_calls[0]
        assert "--exclude=*.lock" in call
        assert "--exclude=.staging" in call
        assert any("ssh" in part and "-p 2222" in part for part in call)
        assert call[-2] == "user@10.0.0.1:/data/tick/20260918/"
        assert call[-1].endswith("20260918/")
        assert result.files == 2
        assert result.skipped == 0
        assert result.bytes == 10

    def test_files_already_present_with_matching_size_are_counted_as_skipped(self, tmp_path):
        listing = "20260918/SHFE/rb2510.parquet\t8\n20260918/report.json\t2\n"
        runner = FakeRunner(lambda args: (0, listing, ""))
        backend = RsyncBackend(_host(), runner)
        existing = tmp_path / "staging" / "20260918" / "SHFE" / "rb2510.parquet"
        existing.parent.mkdir(parents=True)
        existing.write_bytes(b"12345678")

        result = backend.fetch_day("20260918", tmp_path / "staging")

        assert result.skipped == 1
        assert result.files == 1
        assert result.bytes == 2

    def test_rsync_failure_raises(self, tmp_path):
        runner = FakeRunner(
            lambda args: (
                (0, "20260918/SHFE/rb2510.parquet\t8\n", "")
                if args[0] == "ssh"
                else (23, "", "rsync error")
            )
        )
        backend = RsyncBackend(_host(), runner)

        with pytest.raises(PullError) as error:
            backend.fetch_day("20260918", tmp_path / "staging")

        assert "rsync" in str(error.value)


class TestDelete:
    def test_delete_runs_rm_rf_on_the_day_path(self):
        runner = FakeRunner(lambda args: (0, "", ""))
        backend = RsyncBackend(_host(), runner)

        backend.delete_day("20260918")

        command = runner.ssh_calls[-1][-1]
        assert command.startswith("rm -rf -- ")
        assert "/data/tick/20260918" in command

    def test_delete_refuses_a_path_that_is_not_a_day(self):
        runner = FakeRunner()
        backend = RsyncBackend(_host(), runner)

        with pytest.raises(PullError):
            backend.delete_day("/data/tick")

        assert runner.calls == []


class TestCheck:
    def test_check_reports_ok(self):
        backend = RsyncBackend(_host(), FakeRunner(lambda args: (0, "", "")))

        status = backend.check()

        assert status.ok is True
        assert status.name == "linux-a"

    def test_check_reports_failure(self):
        backend = RsyncBackend(_host(), FakeRunner(lambda args: (255, "", "no route")))

        status = backend.check()

        assert status.ok is False
        assert "no route" in status.detail
