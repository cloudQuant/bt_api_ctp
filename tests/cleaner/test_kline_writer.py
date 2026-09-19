"""离线契约测试：K 线跨日追加、原子替换与品种目录布局。"""

from __future__ import annotations

from calendar import timegm
from datetime import datetime

import pyarrow.parquet as pq

from bt_api_ctp.cleaner.kline.schema import Bar
from bt_api_ctp.cleaner.kline.writer import append_bars, kline_path


def _ns(action_day: str, update_time: str) -> int:
    moment = datetime.strptime(f"{action_day} {update_time}", "%Y%m%d %H:%M:%S")
    return timegm(moment.timetuple()) * 1_000_000_000


def _bar(minute: int, close: float, *, volume: int = 1, day: str = "20260918") -> Bar:
    return Bar(
        datetime=_ns(day, f"09:{minute:02d}:00"),
        open=close,
        high=close,
        low=close,
        close=close,
        volume=volume,
        amount=float(volume) * close,
        open_interest=1000.0,
        trading_day=day,
        exchange_id="SHFE",
        instrument_id="rb2510",
    )


class TestKlinePath:
    def test_layout_is_exchange_symbol_instrument_period(self, tmp_path):
        path = kline_path(tmp_path, "SHFE", "rb", "rb2510", 5)

        assert path == tmp_path / "SHFE" / "rb" / "rb2510_5min.parquet"

    def test_option_goes_under_its_underlying_symbol(self, tmp_path):
        path = kline_path(tmp_path, "DCE", "m", "m2509-C-3000", 1)

        assert path == tmp_path / "DCE" / "m" / "m2509-C-3000_1min.parquet"


class TestAppendBars:
    def test_creates_file_sorted_by_bucket(self, tmp_path):
        path = tmp_path / "SHFE" / "rb" / "rb2510_1min.parquet"

        total = append_bars(path, [_bar(1, 102.0), _bar(0, 101.0)])

        rows = pq.read_table(path).to_pylist()
        assert total == 2
        assert [row["close"] for row in rows] == [101.0, 102.0]

    def test_rerun_of_the_same_day_does_not_duplicate(self, tmp_path):
        path = tmp_path / "SHFE" / "rb" / "rb2510_1min.parquet"
        append_bars(path, [_bar(0, 101.0), _bar(1, 102.0)])

        total = append_bars(path, [_bar(0, 101.0), _bar(1, 102.0)])

        assert total == 2
        assert pq.ParquetFile(path).metadata.num_rows == 2

    def test_same_bucket_is_overwritten_by_the_newer_write(self, tmp_path):
        path = tmp_path / "SHFE" / "rb" / "rb2510_1min.parquet"
        append_bars(path, [_bar(0, 101.0)])

        append_bars(path, [_bar(0, 999.0)])

        rows = pq.read_table(path).to_pylist()
        assert len(rows) == 1
        assert rows[0]["close"] == 999.0

    def test_second_day_appends_after_the_first(self, tmp_path):
        path = tmp_path / "SHFE" / "rb" / "rb2510_1min.parquet"
        append_bars(path, [_bar(0, 101.0)])

        total = append_bars(path, [_bar(0, 201.0, day="20260921")])

        rows = pq.read_table(path).to_pylist()
        assert total == 2
        assert [row["trading_day"] for row in rows] == ["20260918", "20260921"]

    def test_empty_bars_do_not_create_a_file(self, tmp_path):
        path = tmp_path / "SHFE" / "rb" / "rb2510_1min.parquet"

        assert append_bars(path, []) == 0
        assert not path.exists()

    def test_file_carries_the_fixed_schema(self, tmp_path):
        path = tmp_path / "SHFE" / "rb" / "rb2510_1min.parquet"
        append_bars(path, [_bar(0, 101.0)])

        schema = pq.ParquetFile(path).schema_arrow
        assert schema.names == [
            "datetime",
            "trading_day",
            "exchange_id",
            "instrument_id",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "amount",
            "open_interest",
        ]
