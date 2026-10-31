"""CTP native field normalization contracts."""

import types

import pytest

from bt_api_ctp import fields


@pytest.mark.parametrize(
    ("value", "expected"),
    (("0", "buy"), ("1", "sell"), ("buy", "buy"), ("LONG", "buy"), ("s", "sell"), (None, "buy")),
)
def test_direction_normalization(value, expected):
    assert fields.ctp_direction(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    (("0", "open"), ("1", "close"), ("3", "close_today"), ("4", "close_yesterday"), ("bad", "open")),
)
def test_offset_normalization(value, expected):
    assert fields.ctp_offset(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    (("2", "long"), ("3", "short"), ("buy", "long"), ("sell", "short"), ("bad", "long")),
)
def test_position_direction_normalization(value, expected):
    assert fields.ctp_position_direction(value) == expected


def test_order_status_lets_submit_rejection_win():
    assert fields.normalize_order_status("0") == "completed"
    assert fields.normalize_order_status("1") == "partial"
    assert fields.normalize_order_status("0", "4") == "rejected"
    assert fields.normalize_order_status("0", "5") == "cancel_rejected"
    assert fields.normalize_order_status(None, None, "submitted") == "submitted"


def test_safe_field_attr_handles_dicts_structs_and_failures():
    class Broken:
        @property
        def password(self):
            raise RuntimeError("boom")

    assert fields.safe_field_attr({"InstrumentID": "rb2510"}, "InstrumentID") == "rb2510"
    assert fields.safe_field_attr(types.SimpleNamespace(Volume=2), "Volume") == 2
    assert fields.safe_field_attr(Broken(), "password", "fallback") == "fallback"
    assert fields.safe_text_attr(Broken(), "password", default="fallback") == "fallback"


def test_field_to_dict_and_extract_fields_skip_callables():
    field = types.SimpleNamespace(
        InstrumentID="rb2510",
        OrderRef="42",
        method=lambda: None,
        _private="ignored",
    )

    full = fields.field_to_dict(field)
    assert full["InstrumentID"] == "rb2510"
    assert "method" not in full
    assert "_private" not in full

    subset = fields.extract_fields(field, ("OrderRef", "method", "missing"))
    assert subset == {"OrderRef": "42"}


def test_query_record_fields_are_unique_and_cover_order_and_trade():
    assert len(fields.QUERY_RECORD_FIELDS) == len(set(fields.QUERY_RECORD_FIELDS))
    assert set(fields.ORDER_FIELDS).issubset(fields.QUERY_RECORD_FIELDS)
    assert set(fields.TRADE_FIELDS).issubset(fields.QUERY_RECORD_FIELDS)
    assert "PriceTick" in fields.QUERY_RECORD_FIELDS
