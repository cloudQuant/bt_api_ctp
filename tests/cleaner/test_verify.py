"""离线契约测试：拉取后的三级校验（清单/字节/可读）。"""

from __future__ import annotations

import pyarrow as pa
import pyarrow.parquet as pq

from bt_api_ctp.cleaner.pull.backend import RemoteFile
from bt_api_ctp.cleaner.verify import verify_staged_day
from bt_api_ctp.collector.sink import TICK_ARROW_SCHEMA


def _tick_row():
    return {
        "trading_day": "20260918",
        "action_day": "20260918",
        "update_time": "09:00:00",
        "update_millisec": 0,
        "exchange_id": "SHFE",
        "instrument_id": "rb2510",
        "local_receive_time": 1,
        "last_price": 3500.0,
    }


def _stage(tmp_path, rows=2):
    staging = tmp_path / "staging"
    day_dir = staging / "20260918"
    parquet = day_dir / "SHFE" / "rb2510.parquet"
    parquet.parent.mkdir(parents=True)
    table = pa.Table.from_pylist([_tick_row() for _ in range(rows)], schema=TICK_ARROW_SCHEMA)
    pq.write_table(table, parquet)
    (day_dir / "report.json").write_text("{}", encoding="utf-8")
    return staging, parquet


def _remote_files(staging, *, size_override=None):
    files = []
    for path in sorted((staging / "20260918").rglob("*")):
        if path.is_file():
            size = size_override if size_override is not None else path.stat().st_size
            rel = path.relative_to(staging).as_posix()
            files.append(RemoteFile(rel_path=rel, size_bytes=size))
    return files


class TestVerifyStagedDay:
    def test_matching_copy_passes_and_records_rows(self, tmp_path):
        staging, _ = _stage(tmp_path)

        result = verify_staged_day(_remote_files(staging), staging, "20260918")

        assert result.ok is True
        assert result.problems == []
        assert result.total_rows == 2
        parquet_entry = next(f for f in result.files if f["rel_path"].endswith(".parquet"))
        assert parquet_entry["rows"] == 2
        report_entry = next(f for f in result.files if f["rel_path"].endswith("report.json"))
        assert report_entry["rows"] == 0

    def test_missing_file_is_a_problem(self, tmp_path):
        staging, parquet = _stage(tmp_path)
        files = _remote_files(staging)
        parquet.unlink()

        result = verify_staged_day(files, staging, "20260918")

        assert result.ok is False
        assert any(problem.startswith("missing:") for problem in result.problems)

    def test_size_mismatch_is_a_problem(self, tmp_path):
        staging, _ = _stage(tmp_path)

        result = verify_staged_day(_remote_files(staging, size_override=1), staging, "20260918")

        assert result.ok is False
        assert any("size mismatch" in problem for problem in result.problems)

    def test_unreadable_parquet_is_a_problem(self, tmp_path):
        staging, parquet = _stage(tmp_path)
        parquet.write_bytes(b"not parquet")
        files = _remote_files(staging)

        result = verify_staged_day(files, staging, "20260918")

        assert result.ok is False
        assert any(problem.startswith("unreadable:") for problem in result.problems)

    def test_unexpected_local_file_is_a_problem(self, tmp_path):
        staging, _ = _stage(tmp_path)
        files = _remote_files(staging)
        (staging / "20260918" / "SHFE" / "stale.parquet").write_bytes(b"stale")

        result = verify_staged_day(files, staging, "20260918")

        assert result.ok is False
        assert any("unexpected local file" in problem for problem in result.problems)

    def test_missing_staging_directory_reports_every_remote_file(self, tmp_path):
        result = verify_staged_day(
            [RemoteFile(rel_path="20260918/SHFE/rb2510.parquet", size_bytes=10)],
            tmp_path / "staging",
            "20260918",
        )

        assert result.ok is False
        assert result.problems == ["missing: 20260918/SHFE/rb2510.parquet"]
