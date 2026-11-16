"""Fake async native query races and product identity regression tests."""

from decimal import Decimal
from types import MappingProxyType

import pytest
from test_iter62_settlement_readback_on_write import _login
from test_iter63_gateway_instrument_query import KEY, _probe_on_other_thread, finish, row, setup

import bt_api_ctp.gateway_instrument_query as instrument_query
from bt_api_ctp.gateway_instrument_query import CtpGatewayInstrumentQueryError


def test_validated_row_cannot_be_replaced_during_rules_projection(monkeypatch):
    reader, trader, native = setup()
    native.rows = [row()]
    original_poll = trader.poll_instruments_query
    original_validate = reader._validate_result
    original_rules = instrument_query._rules
    captured = []
    mutated = []

    def capture(handle):
        result = original_poll(handle)
        if result is not None:
            captured.append(result)
        return result

    def validate_then_mutate(*args):
        source = original_validate(*args)
        if captured and not mutated:
            captured[0].records[0].update(ProductID="wrong", PriceTick=99.0)
            mutated.append(True)
        return source

    def project_then_restore(native_row, instrument):
        result = original_rules(native_row, instrument)
        captured[0].records[0].update(ProductID="rb", PriceTick=1.0)
        return result

    monkeypatch.setattr(trader, "poll_instruments_query", capture)
    monkeypatch.setattr(reader, "_validate_result", validate_then_mutate)
    monkeypatch.setattr(instrument_query, "_rules", project_then_restore)
    observation = finish(reader, reader.begin(KEY))
    assert observation.rules["base_currency"] == "rb"
    assert observation.rules["price_tick"] == Decimal("1.0")
    assert observation.product_id == "rb"
    assert len(native.requests) == 1


@pytest.mark.parametrize("product", ["r", "rb2", "other_product"])
def test_explicit_product_comes_from_native_row_not_symbol(product):
    reader, _, native = setup()
    native.rows = [row(ProductID=product)]
    observation = finish(reader, reader.begin(KEY))
    assert observation.product_id == product
    assert len(native.requests) == 1
    reader.assert_current(observation)
    assert _probe_on_other_thread(reader, observation) is True


@pytest.mark.parametrize("product", [None, True, 1, b"rb", " ", "rb\n", "x" * 257, "\ud800"])
def test_malformed_product_is_refused(product):
    reader, _, native = setup()
    native.rows = [row(ProductID=product)]
    with pytest.raises(CtpGatewayInstrumentQueryError):
        finish(reader, reader.begin(KEY))


def test_replacing_product_and_rules_fails_both_observation_checks():
    reader, _, native = setup()
    native.rows = [row()]
    observation = finish(reader, reader.begin(KEY))
    original_rules = observation.rules
    object.__setattr__(observation, "product_id", "wrong")
    with pytest.raises(CtpGatewayInstrumentQueryError, match="OBSERVATION_UNTRUSTED"):
        reader.assert_current(observation)
    assert _probe_on_other_thread(reader, observation) is False
    object.__setattr__(observation, "product_id", "rb")
    changed = dict(original_rules)
    changed["price_tick"] = Decimal("2")
    object.__setattr__(observation, "rules", MappingProxyType(changed))
    with pytest.raises(CtpGatewayInstrumentQueryError, match="OBSERVATION_UNTRUSTED"):
        reader.assert_current(observation)
    assert _probe_on_other_thread(reader, observation) is False
    object.__setattr__(observation, "rules", original_rules)
    reader.assert_current(observation)
    assert _probe_on_other_thread(reader, observation) is True


def test_new_trading_day_and_session_stale_previous_product_observation():
    reader, trader, native = setup()
    native.rows = [row()]
    observation = finish(reader, reader.begin(KEY))
    assert observation.product_id == "rb"
    _login(trader, native, session_id=20, trading_day="20990101")
    with pytest.raises(CtpGatewayInstrumentQueryError):
        reader.assert_current(observation)
    assert _probe_on_other_thread(reader, observation) is False


def test_nested_original_row_changes_do_not_mutate_verified_snapshot():
    reader, _, native = setup()
    nested = {"levels": [{"value": "old"}]}
    native.rows = [row(Extra=nested)]
    observation = finish(reader, reader.begin(KEY))
    nested["levels"][0]["value"] = "new"
    assert observation._result.records[0]["Extra"]["levels"][0]["value"] == "old"
    with pytest.raises(TypeError):
        observation._result.records[0]["Extra"]["levels"][0]["value"] = "new"
    reader.assert_current(observation)
    assert _probe_on_other_thread(reader, observation) is True


class _Unsupported:
    def __repr__(self):
        raise AssertionError("unsupported object must not be rendered")


@pytest.mark.parametrize("extra", [_Unsupported(), "x" * (256 * 1024 + 1), list(range(4097))])
def test_unbounded_or_unsupported_native_rows_fail_closed(extra):
    reader, _, native = setup()
    native.rows = [row(Extra=extra)]
    with pytest.raises(CtpGatewayInstrumentQueryError, match="QUERY_UNTRUSTED"):
        finish(reader, reader.begin(KEY))
