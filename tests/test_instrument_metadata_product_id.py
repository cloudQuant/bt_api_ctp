"""Offline ProductID projection tests using the native query-source fixture."""

from dataclasses import FrozenInstanceError, replace
from traceback import format_exception

import pytest
from test_instrument_metadata import _EXCHANGE, _INSTRUMENT, _reader

from bt_api_ctp.instrument_metadata import CtpInstrumentMetadataError

_MISSING = object()


def _read(reader):
    return reader.read(
        _INSTRUMENT, _EXCHANGE, account_session_generation="account-generation-7", timeout=2.0
    )


def _set_returned_row(monkeypatch, reader, *, product=_MISSING, exchange=_EXCHANGE):
    trader = reader.request_feed.trader_client
    callback = trader._handle_query_callback

    def with_product(request_type, row, *args):
        row.ExchangeID = exchange
        if product is not _MISSING:
            row.ProductID = product
        return callback(request_type, row, *args)

    monkeypatch.setattr(trader, "_handle_query_callback", with_product)


def test_product_is_from_same_certified_row_and_does_not_change_v1_wire(monkeypatch):
    reader, api, _ = _reader(monkeypatch)
    _set_returned_row(monkeypatch, reader, product="IF")
    fact = _read(reader)
    assert fact.product_id == "IF"
    assert fact.product_class == "1"
    assert api.query_fields == [(_INSTRUMENT, _EXCHANGE, "")]
    with pytest.raises(FrozenInstanceError):
        fact.product_id = "other"
    with pytest.raises(ValueError):
        replace(fact, product_id="other")
    assert set(fact.to_dict()) == {
        "schema_id", "schema_version", "instrument_id", "exchange_id", "product_class",
        "asset_type", "price_tick", "volume_multiple", "lower_limit_price", "upper_limit_price",
        "price_limits_available", "price_limits_source", "account_session_generation",
        "account_fingerprint", "trading_day", "source_connection_generation", "source_request_id",
        "source_records_sha256", "source_revision", "source", "query_started_at_utc",
        "observed_at_utc", "query_clock_domain_id", "query_elapsed_seconds",
        "quote_stream_generation", "quote_source", "clock_domain_id", "clock_source_session_id",
        "clock_source",
    }
    assert fact.to_dict()["schema_version"] == 1


@pytest.mark.parametrize("product", [_MISSING, None, ""])
def test_absent_product_is_none_without_symbol_or_class_fallback(monkeypatch, product):
    reader, _, _ = _reader(monkeypatch)
    _set_returned_row(monkeypatch, reader, product=product)
    assert _read(reader).product_id is None


@pytest.mark.parametrize("product", [True, 12, b"\xff", "x" * 129, "x\n", " x", "x\x7f"])
def test_malformed_product_rejected_with_finite_error(monkeypatch, product):
    reader, _, _ = _reader(monkeypatch)
    _set_returned_row(monkeypatch, reader, product=product)
    with pytest.raises(CtpInstrumentMetadataError) as caught:
        _read(reader)
    assert caught.value.code == "PRODUCT_ID_INVALID"
    assert str(caught.value) == "PRODUCT_ID_INVALID"


def test_invalid_unicode_is_rejected_with_finite_error(monkeypatch):
    reader, _, _ = _reader(monkeypatch)
    _set_returned_row(monkeypatch, reader, product="private_product_\ud800")
    with pytest.raises(CtpInstrumentMetadataError) as caught:
        _read(reader)
    assert caught.value.code == "INSTRUMENT_QUERY_UNPROVEN"
    assert str(caught.value) == caught.value.code
    assert "private_product_" not in "".join(format_exception(caught.value))


@pytest.mark.parametrize("instrument,exchange", [("IH2612", _EXCHANGE), (_INSTRUMENT, "SHFE")])
def test_product_does_not_bypass_exact_contract_or_venue(monkeypatch, instrument, exchange):
    reader, _, _ = _reader(monkeypatch, returned_instrument=instrument)
    _set_returned_row(monkeypatch, reader, product="IF", exchange=exchange)
    with pytest.raises(CtpInstrumentMetadataError, match="^INSTRUMENT_QUERY_UNPROVEN$"):
        _read(reader)


@pytest.mark.parametrize("tamper", ["product", "source"])
def test_product_cannot_be_swapped_under_same_query_hash_or_corrupt_source(monkeypatch, tamper):
    reader, _, _ = _reader(monkeypatch)
    _set_returned_row(monkeypatch, reader, product="IF")
    feed = reader.request_feed
    original = feed.query_instruments_result

    def modified_result(**kwargs):
        result = original(**kwargs)
        if tamper == "product":
            row = dict(result.records[0])
            row["ProductID"] = "IH"
            changed = replace(result, records=(row,))
            object.__setattr__(changed, "_source", result.query_source)
            return changed
        return replace(result)

    monkeypatch.setattr(feed, "query_instruments_result", modified_result)
    with pytest.raises(CtpInstrumentMetadataError, match="^INSTRUMENT_QUERY_UNPROVEN$"):
        _read(reader)
