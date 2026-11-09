"""CTP SessionID is a signed 32-bit int (iteration 63, SimNow 63.09 finding).

SimNow issued a negative SessionID at login; every positive-only identity check
then refused prepared placement (``ctp_prepared_cancel_session_unavailable``)
although the session was valid. Zero stays the absent sentinel.
"""

import pytest

from bt_api_ctp.cancel_target_projection import validate_ctp_cancel_target_fields
from bt_api_ctp.gateway_cancel_source import cancel_attempt_key
from bt_api_ctp.gateway_place_source import place_attempt_key
from bt_api_ctp.native_session_id import (
    CTP_SESSION_ID_MAX,
    CTP_SESSION_ID_MIN,
    is_ctp_session_id,
    parse_ctp_session_id,
)

NEGATIVE = -2069364652  # shape of a real SimNow login SessionID


@pytest.mark.parametrize(
    "value", [1, NEGATIVE, CTP_SESSION_ID_MIN, CTP_SESSION_ID_MAX, -1]
)
def test_signed_nonzero_32bit_values_are_sessions(value):
    assert is_ctp_session_id(value) is True


@pytest.mark.parametrize(
    "value", [0, True, False, CTP_SESSION_ID_MAX + 1, CTP_SESSION_ID_MIN - 1, 1.0, "1", None]
)
def test_absent_or_out_of_range_values_are_rejected(value):
    assert is_ctp_session_id(value) is False


def test_parse_accepts_signed_decimal_text_only():
    assert parse_ctp_session_id("-2069364652") == NEGATIVE
    assert parse_ctp_session_id("0042") == 42
    assert parse_ctp_session_id(NEGATIVE) == NEGATIVE
    for raw in ("0", "-0", "-", "", "+5", "1.5", " 7", "２", str(2**31), True):
        assert parse_ctp_session_id(raw) is None


def _source(schema):
    return {
        "schema": schema,
        "source_ref": "ctp-query-source-v1:" + "a" * 64,
        "source_tags_digest": "b" * 64,
        "login_request_id": 1,
    }


def test_place_attempt_key_accepts_negative_session_and_rejects_zero():
    common = dict(broker="9999", investor="investor", day="20261110", front=1,
                  request_id=7, order_ref="000000000001",
                  source_binding=_source("ctp-place-source-v1"))
    key = place_attempt_key(session=NEGATIVE, **common)
    assert key != place_attempt_key(session=-NEGATIVE, **common)
    with pytest.raises(ValueError, match="INVALID_PLACE_SOURCE"):
        place_attempt_key(session=0, **common)
    with pytest.raises(ValueError, match="INVALID_PLACE_SOURCE"):
        place_attempt_key(session=2**31, **common)


def test_cancel_attempt_key_accepts_negative_session_and_rejects_zero():
    common = dict(broker="9999", investor="investor", day="20261110", front=1,
                  request_id=7, action_ref=3,
                  source_binding=_source("ctp-cancel-source-v1"))
    assert cancel_attempt_key(session=NEGATIVE, **common)
    with pytest.raises(ValueError, match="INVALID_CANCEL_SOURCE"):
        cancel_attempt_key(session=0, **common)


class _Target:
    exchange_id = "SHFE"
    order_sys_id = "12345"
    front_id = 1
    session_id = NEGATIVE


def test_cancel_target_with_negative_session_passes_identity_shape():
    class Boom(Exception):
        pass

    target = _Target()
    try:
        validate_ctp_cancel_target_fields(target, {}, error_type=Boom)
    except Boom as error:
        # The identity shape passed; any refusal must come from field matching.
        assert "invalid exact CTP cancel target" not in str(error)
    except AttributeError:
        pass  # Past the shape check: the stub lacks the request-field attributes.
    target.session_id = 0
    with pytest.raises(Boom, match="invalid exact CTP cancel target"):
        validate_ctp_cancel_target_fields(target, {}, error_type=Boom)
