"""端到端集成测试：假交易所跑通主流程，证明主流程与 CTP 解耦（AC-8）。

本测试不 import 任何 CTP 具体实现：假交易所只需要满足
InstrumentProvider / MarketDataSubscriber 协议，并在 subscriber 内部组合
自己的 normalizer。若主流程混入了交易所专有依赖，这里会直接失败。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pyarrow.parquet as pq

import bt_api_ctp.collector as collector_package
from bt_api_ctp.collector.engine import CollectionConfig, TickCollectionEngine
from bt_api_ctp.collector.protocols import InstrumentSpec, TickRecord
from bt_api_ctp.collector.shard import ShardConfig

FAKE_EXCHANGE = "FAKE"


class _FakeNormalizer:
    """把假交易所原生 payload 归一化为 TickRecord。"""

    def normalize(self, raw):
        symbol = raw.get("symbol")
        if not symbol:
            return None
        return TickRecord(
            exchange_id=FAKE_EXCHANGE,
            instrument_id=symbol,
            trading_day=raw["trading_day"],
            action_day=raw["action_day"],
            update_time=raw["update_time"],
            update_millisec=raw["millisec"],
            local_receive_time=raw["recv"],
            last_price=raw.get("last"),
        )


class _FakeExchange:
    """一个完全不依赖 CTP 的假交易所（同时充当 provider 与 subscriber）。"""

    def __init__(self, specs, payloads):
        self._specs = list(specs)
        self._payloads = list(payloads)
        self._normalizer = _FakeNormalizer()
        self.handler = None
        self.connected = False
        self.closed = False
        self.subscribed = None

    # InstrumentProvider
    def fetch_instruments(self):
        return list(self._specs)

    # MarketDataSubscriber
    def set_handler(self, handler):
        self.handler = handler

    def connect(self):
        self.connected = True
        for payload in self._payloads:
            tick = self._normalizer.normalize(payload)
            if tick is not None:
                self.handler.on_tick(tick)

    def subscribe(self, instruments):
        self.subscribed = list(instruments)

    def run(self):
        return None

    def close(self):
        self.closed = True


def _payload(
    symbol: str = "FAKE-A",
    *,
    update_time: str = "09:00:00",
    millisec: int = 0,
    recv: int = 1,
    last: float | None = 10.0,
) -> dict:
    return {
        "symbol": symbol,
        "trading_day": "20260916",
        "action_day": "20260916",
        "update_time": update_time,
        "millisec": millisec,
        "recv": recv,
        "last": last,
    }


def _fake_config(tmp_path):
    return CollectionConfig(
        data_root=tmp_path,
        shard=ShardConfig(strategy="by_exchange", exchanges=(FAKE_EXCHANGE,)),
        flush_interval_sec=0.01,
    )


class TestFakeExchangeEndToEnd:
    def test_full_pipeline_persists_clean_data(self, tmp_path):
        specs = [
            InstrumentSpec("FAKE-A", FAKE_EXCHANGE, "future"),
            InstrumentSpec("FAKE-B", FAKE_EXCHANGE, "future"),
        ]
        payloads = [
            _payload("FAKE-A", update_time="09:00:01", recv=2),
            _payload("FAKE-A", update_time="09:00:00", recv=1),  # 乱序
            _payload("FAKE-A", update_time="09:00:00", recv=3),  # 重复键 → 保留 recv=3
            _payload("FAKE-B", millisec=500, recv=4),
            _payload("FAKE-B", millisec=500, recv=5),  # 重复键 → 保留 recv=5
            {"symbol": "", "trading_day": "20260916"},  # 无身份 → 丢弃
            _payload("FAKE-A", update_time="09:00:02", recv=6, last=None),  # 价格哨兵
        ]
        exchange = _FakeExchange(specs, payloads)
        engine = TickCollectionEngine(
            provider=exchange, subscriber=exchange, config=_fake_config(tmp_path)
        )

        report = engine.run_once(duration_sec=0.05)

        assert exchange.connected is True
        assert exchange.closed is True
        assert exchange.subscribed == ["FAKE-A", "FAKE-B"]

        a_rows = pq.read_table(
            tmp_path / "20260916" / FAKE_EXCHANGE / "FAKE-A.parquet"
        ).to_pylist()
        assert [(row["update_time"], row["local_receive_time"]) for row in a_rows] == [
            ("09:00:00", 3),
            ("09:00:01", 2),
            ("09:00:02", 6),
        ]
        assert a_rows[2]["last_price"] is None  # 哨兵落地为 NULL

        b_rows = pq.read_table(
            tmp_path / "20260916" / FAKE_EXCHANGE / "FAKE-B.parquet"
        ).to_pylist()
        assert len(b_rows) == 1
        assert b_rows[0]["local_receive_time"] == 5

        assert report.trading_day == "20260916"
        assert {entry.instrument_id: entry.rows for entry in report.instruments} == {
            "FAKE-A": 3,
            "FAKE-B": 1,
        }
        assert (tmp_path / "20260916" / "report.json").exists()

    def test_second_run_merges_without_duplicates(self, tmp_path):
        specs = [InstrumentSpec("FAKE-A", FAKE_EXCHANGE, "future")]
        payloads = [_payload("FAKE-A", recv=1)]

        for _ in range(2):
            exchange = _FakeExchange(specs, payloads)
            engine = TickCollectionEngine(
                provider=exchange, subscriber=exchange, config=_fake_config(tmp_path)
            )
            engine.run_once(duration_sec=0.05)

        rows = pq.read_table(
            tmp_path / "20260916" / FAKE_EXCHANGE / "FAKE-A.parquet"
        ).to_pylist()
        assert len(rows) == 1


class TestCollectorPackageIsVenueAgnostic:
    """主流程模块不得依赖任何交易所具体实现（AC-8 的机器可验证证据）。"""

    PURE_MODULES = (
        "protocols.py",
        "engine.py",
        "buffer.py",
        "sink.py",
        "shard.py",
        "schedule.py",
        "protocols.py",
    )
    FORBIDDEN_PREFIXES = ("bt_api_ctp.ctp", "bt_api_ctp.collector_ctp", "_ctp")

    def test_no_exchange_imports_in_core_modules(self):
        base = Path(collector_package.__file__).parent
        offenders: list[str] = []
        for name in self.PURE_MODULES:
            path = base / name
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                modules: list[str] = []
                if isinstance(node, ast.Import):
                    modules = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    modules = [node.module or ""]
                for module in modules:
                    if module.startswith(self.FORBIDDEN_PREFIXES):
                        offenders.append(f"{name}: {module}")
        assert offenders == []

    def test_cli_is_the_only_assembly_point(self):
        """cli.py 允许做具体实现装配，但必须是延迟导入（函数内）。"""
        base = Path(collector_package.__file__).parent
        tree = ast.parse((base / "cli.py").read_text(encoding="utf-8"))
        module_level = [
            node.module
            for node in tree.body
            if isinstance(node, ast.ImportFrom) and node.module
        ]
        for module in module_level:
            assert not module.startswith(self.FORBIDDEN_PREFIXES), module
