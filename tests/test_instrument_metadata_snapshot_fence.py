"""Fake native query interleavings for instrument metadata snapshot binding."""

from datetime import datetime, timezone
from decimal import Decimal

import pytest
from test_instrument_metadata import _reader
from test_instrument_metadata_product_id import _read, _set_returned_row

from bt_api_ctp.instrument_metadata import CtpInstrumentMetadataError
from bt_api_ctp.query import _query_records_digest


def test_post_validation_mutation_cannot_change_projected_values(monkeypatch):
    reader, api, _ = _reader(monkeypatch)
    _set_returned_row(monkeypatch, reader, product="IF")
    trader = reader.request_feed.trader_client
    original_validate = reader._validate_query_result

    def mutate_after_validation(*args, **kwargs):
        checked = original_validate(*args, **kwargs)
        row = trader.get_query_result(api.request_ids[0]).records[0]
        row.update(
            {
                "InstrumentID": "IH2612",
                "ExchangeID": "SHFE",
                "ProductID": "IH",
                "PriceTick": 9.9,
                "VolumeMultiple": 1,
                "ProductClass": "2",
                "LowerLimitPrice": 10.0,
                "UpperLimitPrice": 20.0,
            }
        )
        return checked

    monkeypatch.setattr(reader, "_validate_query_result", mutate_after_validation)
    fact = _read(reader)
    assert (fact.product_id, fact.price_tick, fact.volume_multiple, fact.product_class) == (
        "IF",
        "0.2",
        300,
        "1",
    )
    assert (fact.lower_limit_price, fact.upper_limit_price) == ("1000", "2000")
    assert (fact.instrument_id, fact.exchange_id) == ("IF2612", "CFFEX")


def _with_extra_field(monkeypatch, reader, value):
    trader = reader.request_feed.trader_client
    callback = trader._handle_query_callback

    def with_extra(request_type, row, *args):
        row.Extra = value
        return callback(request_type, row, *args)

    monkeypatch.setattr(trader, "_handle_query_callback", with_extra)


def test_nested_row_is_detached_and_digest_uses_same_frozen_values(monkeypatch):
    reader, api, _ = _reader(monkeypatch)
    _set_returned_row(monkeypatch, reader, product="IF")
    extra = {
        "nested": [{"text": "original", "decimal": Decimal("1.20")}],
        "at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        "binary": b"abc",
    }
    _with_extra_field(monkeypatch, reader, extra)
    original_validate = reader._validate_query_result
    snapshot_seen = []

    def mutate_after_validation(*args, **kwargs):
        source, row = original_validate(*args, **kwargs)
        original = reader.request_feed.trader_client.get_query_result(api.request_ids[0]).records[0]
        original["Extra"]["nested"][0]["text"] = "changed"
        assert row["Extra"]["nested"][0]["text"] == "original"
        with pytest.raises(TypeError):
            row["Extra"]["nested"][0]["text"] = "changed"
        assert _query_records_digest((row,)) == source.records_sha256
        snapshot_seen.append(row)
        return source, row

    monkeypatch.setattr(reader, "_validate_query_result", mutate_after_validation)
    fact = _read(reader)
    assert snapshot_seen and fact.product_id == "IF"
    assert fact.source_records_sha256 == _query_records_digest((snapshot_seen[0],))


def test_mutation_before_current_readback_fails_closed(monkeypatch):
    reader, api, _ = _reader(monkeypatch)
    _set_returned_row(monkeypatch, reader, product="IF")
    trader = reader.request_feed.trader_client
    original_get = trader.get_query_result

    def mutate_before_readback(request_id):
        result = original_get(request_id)
        result.records[0]["ProductID"] = "IH"
        return result

    monkeypatch.setattr(trader, "get_query_result", mutate_before_readback)
    with pytest.raises(CtpInstrumentMetadataError, match="^INSTRUMENT_QUERY_UNPROVEN$"):
        _read(reader)
    assert api.request_ids


class _UnsupportedNativeObject:
    def __repr__(self):
        raise AssertionError("unsupported object repr must not be called")

    def __str__(self):
        raise AssertionError("unsupported object str must not be called")


def _deep_value():
    result = "inner"
    for _ in range(20):
        result = [result]
    return result


@pytest.mark.parametrize(
    "extra", [_UnsupportedNativeObject(), "x" * (256 * 1024 + 1), _deep_value(), list(range(4097))]
)
def test_unsupported_or_unbounded_nested_values_fail_closed(monkeypatch, extra):
    reader, _, _ = _reader(monkeypatch)
    _with_extra_field(monkeypatch, reader, extra)
    with pytest.raises(CtpInstrumentMetadataError) as caught:
        _read(reader)
    assert caught.value.code == "INSTRUMENT_QUERY_UNPROVEN"
