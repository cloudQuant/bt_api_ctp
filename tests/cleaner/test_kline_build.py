"""离线契约测试：按交易日构建 K 线（品种归目录、跳过组合/未知、幂等）。"""

from __future__ import annotations

import pyarrow as pa
import pyarrow.parquet as pq

from bt_api_ctp.cleaner.kline.build import build_day
from bt_api_ctp.cleaner_ctp.symbols import classify_contract
from bt_api_ctp.collector.sink import TICK_ARROW_SCHEMA


def _tick_row(
    instrument_id: str,
    *,
    exchange_id: str,
    update_time: str,
    last_price: float = 100.0,
    volume: int = 10,
    turnover: float = 1000.0,
    millisec: int = 0,
):
    return {
        "trading_day": "20260918",
        "action_day": "20260918",
        "update_time": update_time,
        "update_millisec": millisec,
        "exchange_id": exchange_id,
        "instrument_id": instrument_id,
        "last_price": last_price,
        "volume": volume,
        "turnover": turnover,
        "open_interest": 500.0,
    }


def _write_ticks(tick_root, exchange_id: str, instrument_id: str, rows) -> None:
    directory = tick_root / "20260918" / exchange_id
    directory.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pylist(rows, schema=TICK_ARROW_SCHEMA)
    pq.write_table(table, directory / f"{instrument_id}.parquet")


def _fixture_tree(tmp_path):
    """一份带期货、期权、组合与未知命名的 tick 树。"""
    tick_root = tmp_path / "tick"
    _write_ticks(
        tick_root,
        "SHFE",
        "rb2510",
        [
            _tick_row(
                "rb2510", exchange_id="SHFE", update_time="09:00:00", last_price=3500.0, volume=10
            ),
            _tick_row(
                "rb2510",
                exchange_id="SHFE",
                update_time="09:00:30",
                last_price=3510.0,
                volume=15,
                millisec=500,
            ),
        ],
    )
    _write_ticks(
        tick_root,
        "DCE",
        "m2509-C-3000",
        [
            _tick_row(
                "m2509-C-3000", exchange_id="DCE", update_time="09:00:00", last_price=50.0, volume=1
            ),
            _tick_row(
                "m2509-C-3000", exchange_id="DCE", update_time="09:01:00", last_price=52.0, volume=3
            ),
        ],
    )
    _write_ticks(
        tick_root,
        "CZCE",
        "RM701MSC2100",
        [_tick_row("RM701MSC2100", exchange_id="CZCE", update_time="09:00:00")],
    )
    _write_ticks(
        tick_root,
        "SHFE",
        "??odd-name",
        [_tick_row("??odd-name", exchange_id="SHFE", update_time="09:00:00")],
    )
    return tick_root


class TestBuildDay:
    def test_futures_and_options_land_in_symbol_directories(self, tmp_path):
        tick_root = _fixture_tree(tmp_path)
        kline_root = tmp_path / "kline"

        report = build_day(
            tick_root, kline_root, "20260918", classifier=classify_contract, periods=(1, 5, 15)
        )

        assert (kline_root / "SHFE" / "rb" / "rb2510_1min.parquet").exists()
        assert (kline_root / "SHFE" / "rb" / "rb2510_5min.parquet").exists()
        assert (kline_root / "SHFE" / "rb" / "rb2510_15min.parquet").exists()
        assert (kline_root / "DCE" / "m" / "m2509-C-3000_1min.parquet").exists()
        assert report.instruments == 2

    def test_combination_is_skipped_without_alarm(self, tmp_path):
        tick_root = _fixture_tree(tmp_path)

        report = build_day(tick_root, tmp_path / "kline", "20260918", classifier=classify_contract)

        assert report.skipped_combination == 1
        assert report.skipped_unknown == ["SHFE/??odd-name"]
        assert not (tmp_path / "kline" / "CZCE").exists()

    def test_options_can_be_excluded(self, tmp_path):
        tick_root = _fixture_tree(tmp_path)

        report = build_day(
            tick_root,
            tmp_path / "kline",
            "20260918",
            classifier=classify_contract,
            include_options=False,
        )

        assert report.skipped_option == 1
        assert not (tmp_path / "kline" / "DCE").exists()
        assert (tmp_path / "kline" / "SHFE" / "rb" / "rb2510_1min.parquet").exists()

    def test_one_minute_bars_carry_differenced_volume(self, tmp_path):
        tick_root = _fixture_tree(tmp_path)
        kline_root = tmp_path / "kline"

        build_day(tick_root, kline_root, "20260918", classifier=classify_contract, periods=(1,))

        rows = pq.read_table(kline_root / "SHFE" / "rb" / "rb2510_1min.parquet").to_pylist()
        assert len(rows) == 1
        assert rows[0]["volume"] == 15
        assert rows[0]["open"] == 3500.0
        assert rows[0]["close"] == 3510.0

    def test_rerun_is_idempotent(self, tmp_path):
        tick_root = _fixture_tree(tmp_path)
        kline_root = tmp_path / "kline"

        first_report = build_day(
            tick_root, kline_root, "20260918", classifier=classify_contract, periods=(1, 5, 15)
        )
        first = pq.read_table(kline_root / "SHFE" / "rb" / "rb2510_1min.parquet").to_pylist()
        second_report = build_day(
            tick_root, kline_root, "20260918", classifier=classify_contract, periods=(1, 5, 15)
        )

        second = pq.read_table(kline_root / "SHFE" / "rb" / "rb2510_1min.parquet").to_pylist()
        assert first == second
        assert len(second) == 1
        assert second_report.bars_added == first_report.bars_added

    def test_periods_are_rolled_up_consistently(self, tmp_path):
        tick_root = tmp_path / "tick"
        rows = [
            _tick_row(
                "rb2510",
                exchange_id="SHFE",
                update_time=f"09:0{minute}:00",
                volume=(minute + 1) * 10,
            )
            for minute in range(5)
        ]
        _write_ticks(tick_root, "SHFE", "rb2510", rows)
        kline_root = tmp_path / "kline"

        build_day(
            tick_root, kline_root, "20260918", classifier=classify_contract, periods=(1, 5, 15)
        )

        one = pq.read_table(kline_root / "SHFE" / "rb" / "rb2510_1min.parquet").to_pylist()
        five = pq.read_table(kline_root / "SHFE" / "rb" / "rb2510_5min.parquet").to_pylist()
        fifteen = pq.read_table(kline_root / "SHFE" / "rb" / "rb2510_15min.parquet").to_pylist()
        assert len(one) == 5
        assert len(five) == 1
        assert len(fifteen) == 1
        assert sum(row["volume"] for row in one) == five[0]["volume"] == fifteen[0]["volume"]

    def test_missing_day_directory_yields_an_empty_report(self, tmp_path):
        report = build_day(
            tmp_path / "tick", tmp_path / "kline", "20260918", classifier=classify_contract
        )

        assert report.instruments == 0
        assert report.bars_added == {"1min": 0, "5min": 0, "15min": 0}
