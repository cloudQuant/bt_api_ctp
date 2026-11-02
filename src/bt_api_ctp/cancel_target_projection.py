"""CTP native cancel-target and request-field consistency checks.

The caller retains target identity, session, source and authorization checks.
"""


def validate_ctp_cancel_target_fields(target, request_payload, *, error_type):
    if (
        not isinstance(target.exchange_id, str)
        or not target.exchange_id
        or len(target.exchange_id) > 9
        or not target.exchange_id.isascii()
        or not target.exchange_id.isalnum()
        or not isinstance(target.order_sys_id, str)
        or not target.order_sys_id
        or len(target.order_sys_id) > 21
        or not target.order_sys_id.isascii()
        or target.order_sys_id != target.order_sys_id.strip()
        or type(target.front_id) is not int
        or target.front_id <= 0
        or type(target.session_id) is not int
        or target.session_id <= 0
    ):
        raise error_type("invalid exact CTP cancel target")
    expected = {
        "OrderRef": target.order_ref,
        "ExchangeID": target.exchange_id,
        "OrderSysID": target.order_sys_id,
        "FrontID": target.front_id,
        "SessionID": target.session_id,
    }
    for key, value in expected.items():
        if key not in request_payload or type(request_payload[key]) is not type(value):
            raise error_type("CTP cancel request lacks exact target echo: " + key)
        if request_payload[key] != value:
            raise error_type("CTP cancel request target mismatch: " + key)
    # CTP encodes delete as '0' and modify as '3'. A command whose
    # operation is CANCEL must never carry the valid modify action.
    if (
        type(request_payload.get("ActionFlag")) is not str
        or request_payload["ActionFlag"] != "0"
    ):
        raise error_type(
            "CTP cancel request requires native delete ActionFlag '0'"
        )
    # These fields on CThostFtdcInputOrderActionField are meaningful for
    # modify actions. Leave them at their native zero defaults for deletes.
    limit_price = request_payload.get("LimitPrice", 0.0)
    if type(limit_price) not in (int, float) or limit_price != 0:
        raise error_type("CTP cancel request must not change LimitPrice")
    volume_change = request_payload.get("VolumeChange", 0)
    if type(volume_change) is not int or volume_change != 0:
        raise error_type("CTP cancel request must not change VolumeChange")
    return target
