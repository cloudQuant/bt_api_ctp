"""离线契约测试：CTP 合约全集查询与过滤（复用既有 TraderClient 查询契约）。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from bt_api_ctp.collector_ctp.instrument_provider import CtpInstrumentProvider


@pytest.fixture(autouse=True)
def _no_retry_sleep(monkeypatch):
    """重试退避不应让单元测试真实等待。"""
    monkeypatch.setattr(
        "bt_api_ctp.collector_ctp.instrument_provider.time.sleep", lambda *_: None
    )

FUTURE = {"InstrumentID": "rb2510", "ExchangeID": "SHFE", "ProductClass": "1"}
OPTION = {
    "InstrumentID": "m2701-C-3000",
    "ExchangeID": "DCE",
    "ProductClass": "2",
    "UnderlyingInstrID": "m2701",
    "StrikePrice": 3000.0,
    "OptionsType": "1",
}
COMBINATION = {"InstrumentID": "SPD_rb2510_rb2511", "ExchangeID": "SHFE", "ProductClass": "3"}
FOREIGN_EXCHANGE = {"InstrumentID": "XYZ", "ExchangeID": "UNKNOWN", "ProductClass": "1"}
# CFFEX 股指期权在 CTP 参考数据中为 spot_option（ProductClass=6）
CFFEX_INDEX_OPTION = {
    "InstrumentID": "HO2609-C-2500",
    "ExchangeID": "CFFEX",
    "ProductClass": "6",
    "UnderlyingInstrID": "HO2609",
    "StrikePrice": 2500.0,
    "OptionsType": "1",
}


def _trader(records, *, complete=True, per_exchange=None, incomplete_exchanges=()):
    """Mock trader: per_exchange 为 {exchange: records} 时按交易所返回不同数据。"""
    calls = []

    def query_instruments_result(**kwargs):
        calls.append(kwargs.get("exchange_id"))
        exchange = kwargs.get("exchange_id")
        if exchange in incomplete_exchanges:
            return SimpleNamespace(complete=False, records=())
        if per_exchange is not None:
            payload = per_exchange.get(exchange, [])
            return SimpleNamespace(complete=complete, records=tuple(payload))
        return SimpleNamespace(complete=complete, records=tuple(records))

    return SimpleNamespace(query_instruments_result=query_instruments_result, calls=calls)


class TestCtpInstrumentProvider:
    def test_keeps_futures_and_options_only(self):
        provider = CtpInstrumentProvider(_trader([FUTURE, OPTION, COMBINATION]))
        specs = provider.fetch_instruments()
        assert {spec.instrument_id for spec in specs} == {"rb2510", "m2701-C-3000"}

    def test_preserves_option_identity(self):
        provider = CtpInstrumentProvider(_trader([OPTION]))
        spec = provider.fetch_instruments()[0]
        assert spec.asset_type == "option"
        assert spec.underlying_instrument == "m2701"
        assert spec.strike_price == 3000.0
        assert spec.option_type == "call"

    def test_preserves_future_identity(self):
        provider = CtpInstrumentProvider(_trader([FUTURE]))
        spec = provider.fetch_instruments()[0]
        assert spec.asset_type == "future"
        assert spec.exchange_id == "SHFE"
        assert spec.underlying_instrument is None

    def test_drops_exchanges_outside_the_six(self):
        provider = CtpInstrumentProvider(_trader([FUTURE, FOREIGN_EXCHANGE]))
        specs = provider.fetch_instruments()
        assert {spec.exchange_id for spec in specs} == {"SHFE"}

    def test_includes_ine(self):
        ine = {"InstrumentID": "sc2510", "ExchangeID": "INE", "ProductClass": "1"}
        provider = CtpInstrumentProvider(_trader([ine]))
        assert provider.fetch_instruments()[0].exchange_id == "INE"

    def test_custom_asset_types(self):
        provider = CtpInstrumentProvider(
            _trader([FUTURE, OPTION, COMBINATION]), asset_types=("combination",)
        )
        specs = provider.fetch_instruments()
        assert {spec.instrument_id for spec in specs} == {"SPD_rb2510_rb2511"}

    def test_incomplete_query_fails_closed(self):
        provider = CtpInstrumentProvider(_trader([FUTURE], complete=False))
        with pytest.raises(RuntimeError, match="ctp_instrument_query_incomplete"):
            provider.fetch_instruments()

    def test_queries_each_exchange_separately(self):
        """全市场单次查询会超时，因此必须按交易所分批查询。"""
        trader = _trader([], per_exchange={"SHFE": [FUTURE], "CFFEX": [CFFEX_INDEX_OPTION]})
        provider = CtpInstrumentProvider(trader, exchanges=("SHFE", "CFFEX"))

        specs = provider.fetch_instruments()

        assert trader.calls == ["SHFE", "CFFEX"]
        assert {spec.instrument_id for spec in specs} == {"rb2510", "HO2609-C-2500"}

    def test_index_options_are_collected_by_default(self):
        """中金所股指期权在 CTP 中是 spot_option，默认口径必须包含。"""
        provider = CtpInstrumentProvider(_trader([CFFEX_INDEX_OPTION]))
        specs = provider.fetch_instruments()
        assert len(specs) == 1
        assert specs[0].asset_type == "spot_option"
        assert specs[0].option_type == "call"
        assert specs[0].underlying_instrument == "HO2609"

    def test_one_incomplete_exchange_fails_closed(self):
        trader = _trader([], per_exchange={"SHFE": [FUTURE]}, incomplete_exchanges=("CFFEX",))
        provider = CtpInstrumentProvider(trader, exchanges=("SHFE", "CFFEX"))
        with pytest.raises(RuntimeError, match="CFFEX"):
            provider.fetch_instruments()

    def test_query_timeout_is_configurable(self):
        captured = {}

        def query_instruments_result(**kwargs):
            captured.update(kwargs)
            return SimpleNamespace(complete=True, records=(FUTURE,))

        provider = CtpInstrumentProvider(
            SimpleNamespace(query_instruments_result=query_instruments_result),
            exchanges=("SHFE",),
            query_timeout_sec=42.0,
        )
        provider.fetch_instruments()

        assert captured["timeout"] == 42.0
        assert captured["exchange_id"] == "SHFE"

    def test_empty_records_return_empty_list(self):
        provider = CtpInstrumentProvider(_trader([]))
        assert provider.fetch_instruments() == []

    def test_duplicates_are_removed(self):
        provider = CtpInstrumentProvider(_trader([FUTURE, dict(FUTURE)]))
        specs = provider.fetch_instruments()
        assert len(specs) == 1

    def test_records_without_instrument_id_are_skipped(self):
        nameless = {"ExchangeID": "SHFE", "ProductClass": "1"}
        provider = CtpInstrumentProvider(_trader([nameless, FUTURE]))
        assert {spec.instrument_id for spec in provider.fetch_instruments()} == {"rb2510"}


class TestQueryRetry:
    """并发启动时柜台会拒绝同类型查询，provider 需要退避重试。"""

    def test_retries_until_complete(self):
        calls = []

        def query(**kwargs):
            calls.append(kwargs["exchange_id"])
            complete = len(calls) >= 2  # 第一次 incomplete
            records = (FUTURE,) if complete else ()
            return SimpleNamespace(complete=complete, records=records)

        provider = CtpInstrumentProvider(
            SimpleNamespace(query_instruments_result=query),
            exchanges=("SHFE",),
            query_retries=3,
            retry_backoff_sec=0.0,
        )

        specs = provider.fetch_instruments()

        assert len(calls) == 2
        assert [spec.instrument_id for spec in specs] == ["rb2510"]

    def test_gives_up_after_configured_retries(self):
        calls = []

        def query(**kwargs):
            calls.append(1)
            return SimpleNamespace(complete=False, records=())

        provider = CtpInstrumentProvider(
            SimpleNamespace(query_instruments_result=query),
            exchanges=("SHFE",),
            query_retries=2,
            retry_backoff_sec=0.0,
        )

        with pytest.raises(RuntimeError, match="SHFE"):
            provider.fetch_instruments()

        assert len(calls) == 3  # 首次 + 2 次重试

    def test_one_exchange_retry_does_not_retry_others_unnecessarily(self):
        calls = []

        def query(**kwargs):
            calls.append(kwargs["exchange_id"])
            return SimpleNamespace(complete=True, records=(FUTURE,))

        provider = CtpInstrumentProvider(
            SimpleNamespace(query_instruments_result=query),
            exchanges=("SHFE", "DCE"),
            query_retries=3,
            retry_backoff_sec=0.0,
        )

        provider.fetch_instruments()

        assert calls == ["SHFE", "DCE"]
