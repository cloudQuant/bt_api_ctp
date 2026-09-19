"""离线契约测试：跨机合并去重与 staging 遍历。"""

from __future__ import annotations

import pyarrow as pa
import pyarrow.parquet as pq

from bt_api_ctp.cleaner.merge import drop_staging_day, merge_day, merge_instrument
from bt_api_ctp.collector.sink import TICK_ARROW_SCHEMA


def _row(
    *,
    instrument_id: str = "rb2510",
    exchange_id: str = "SHFE",
    action_day: str = "20260918",
    update_time: str = "09:00:00",
    millisec: int = 0,
    local_receive_time: int = 1,
    last_price: float = 3500.0,
):
    return {
        "trading_day": "20260918",
        "action_day": action_day,
        "update_time": update_time,
        "update_millisec": millisec,
        "exchange_id": exchange_id,
        "instrument_id": instrument_id,
        "local_receive_time": local_receive_time,
        "last_price": last_price,
    }


def _write(path, rows) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows, schema=TICK_ARROW_SCHEMA), path)


def _stage(staging_root, host: str, day: str, exchange: str, instrument: str, rows):
    path = staging_root / host / day / exchange / f"{instrument}.parquet"
    _write(path, rows)
    return path


class TestMergeInstrument:
    def test_new_instrument_is_created(self, tmp_path):
        source = _stage(tmp_path / "staging", "a", "20260918", "SHFE", "rb2510", [_row()])
        target = tmp_path / "tick" / "20260918" / "SHFE" / "rb2510.parquet"

        stat, drift = merge_instrument(target, [source])

        assert (stat.added, stat.deduped, stat.total) == (1, 0, 1)
        assert drift == []
        assert pq.ParquetFile(target).metadata.num_rows == 1

    def test_overlapping_tick_keeps_the_newest_receive(self, tmp_path):
        source = _stage(
            tmp_path / "staging",
            "b",
            "20260918",
            "SHFE",
            "rb2510",
            [
                _row(local_receive_time=200, last_price=2.0),
                _row(update_time="09:01:00", local_receive_time=210, last_price=3.0),
            ],
        )
        target = tmp_path / "tick" / "20260918" / "SHFE" / "rb2510.parquet"
        _write(target, [_row(local_receive_time=100, last_price=1.0)])

        stat, _ = merge_instrument(target, [source])

        rows = pq.read_table(target).to_pylist()
        assert stat.added == 1
        assert stat.deduped == 1
        assert [(row["update_time"], row["last_price"]) for row in rows] == [
            ("09:00:00", 2.0),
            ("09:01:00", 3.0),
        ]

    def test_rows_are_sorted_by_exchange_time(self, tmp_path):
        source = _stage(
            tmp_path / "staging",
            "a",
            "20260918",
            "SHFE",
            "rb2510",
            [
                _row(update_time="09:02:00"),
                _row(update_time="09:00:00"),
                _row(update_time="09:01:00"),
            ],
        )
        target = tmp_path / "tick" / "20260918" / "SHFE" / "rb2510.parquet"

        merge_instrument(target, [source])

        times = [row["update_time"] for row in pq.read_table(target).to_pylist()]
        assert times == ["09:00:00", "09:01:00", "09:02:00"]

    def test_merge_is_idempotent(self, tmp_path):
        source = _stage(tmp_path / "staging", "a", "20260918", "SHFE", "rb2510", [_row()])
        target = tmp_path / "tick" / "20260918" / "SHFE" / "rb2510.parquet"
        merge_instrument(target, [source])

        stat, _ = merge_instrument(target, [source])

        assert stat.added == 0
        assert stat.total == 1

    def test_two_hosts_are_both_merged(self, tmp_path):
        source_a = _stage(
            tmp_path / "staging", "a", "20260918", "SHFE", "rb2510", [_row(update_time="09:00:00")]
        )
        source_b = _stage(
            tmp_path / "staging", "b", "20260918", "SHFE", "rb2510", [_row(update_time="09:01:00")]
        )
        target = tmp_path / "tick" / "20260918" / "SHFE" / "rb2510.parquet"

        stat, _ = merge_instrument(target, [source_a, source_b])

        assert stat.total == 2

    def test_missing_column_is_filled_and_reported(self, tmp_path):
        partial_schema = pa.schema(
            [
                pa.field("trading_day", pa.string()),
                pa.field("action_day", pa.string()),
                pa.field("update_time", pa.string()),
                pa.field("update_millisec", pa.int32()),
                pa.field("local_receive_time", pa.int64()),
                pa.field("exchange_id", pa.string()),
                pa.field("instrument_id", pa.string()),
            ]
        )
        source = tmp_path / "staging" / "a" / "20260918" / "SHFE" / "rb2510.parquet"
        source.parent.mkdir(parents=True)
        pq.write_table(pa.Table.from_pylist([_row()], schema=partial_schema), source)
        target = tmp_path / "tick" / "20260918" / "SHFE" / "rb2510.parquet"

        stat, drift = merge_instrument(target, [source])

        assert stat.total == 1
        assert any("missing" in note for note in drift)
        assert pq.read_table(target).to_pylist()[0]["last_price"] is None


class TestMergeDay:
    def test_merges_every_staged_host_and_instrument(self, tmp_path):
        staging = tmp_path / "staging"
        _stage(staging, "a", "20260918", "SHFE", "rb2510", [_row(update_time="09:00:00")])
        _stage(staging, "b", "20260918", "SHFE", "rb2510", [_row(update_time="09:01:00")])
        _stage(
            staging,
            "b",
            "20260918",
            "DCE",
            "m2509",
            [_row(instrument_id="m2509", exchange_id="DCE")],
        )
        tick_root = tmp_path / "tick"

        report = merge_day(tick_root, staging, "20260918")

        assert report.instruments == 2
        assert report.total == 3
        assert (tick_root / "20260918" / "SHFE" / "rb2510.parquet").exists()
        assert (tick_root / "20260918" / "DCE" / "m2509.parquet").exists()

    def test_other_days_are_ignored(self, tmp_path):
        staging = tmp_path / "staging"
        _stage(staging, "a", "20260917", "SHFE", "rb2510", [_row()])
        _stage(staging, "a", "20260918", "SHFE", "rb2510", [_row()])

        report = merge_day(tmp_path / "tick", staging, "20260918")

        assert report.instruments == 1
        assert not (tmp_path / "tick" / "20260917").exists()

    def test_missing_staging_yields_empty_report(self, tmp_path):
        report = merge_day(tmp_path / "tick", tmp_path / "staging", "20260918")

        assert report.instruments == 0
        assert report.stats == []


class TestDropStagingDay:
    def test_removes_only_the_merged_day(self, tmp_path):
        staging = tmp_path / "staging"
        _stage(staging, "a", "20260918", "SHFE", "rb2510", [_row()])
        _stage(staging, "a", "20260917", "SHFE", "rb2510", [_row()])

        drop_staging_day(staging, "20260918")

        assert not (staging / "a" / "20260918").exists()
        assert (staging / "a" / "20260917" / "SHFE" / "rb2510.parquet").exists()
