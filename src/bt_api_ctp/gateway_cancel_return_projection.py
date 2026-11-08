"""CTP cancellation error projection after caller-verified source capture.

The caller must first assert that the exact offered record is current, then
match the key to a durable prepared attempt. These labels prove no vendor
callback enqueue time, cancellation success, or order status.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from bt_api_base.direct_cancel_protocol import PreparedCancelRequest
from bt_api_base.direct_write_protocol import DirectExecutionEventKind
from bt_api_base.gateway_execution_protocol import GatewayReturnFact
from bt_api_base.market_protocol import MarketInstrumentKey

from bt_api_ctp.gateway_cancel_source import (
    cancel_attempt_key,
    source_tags_digest,
    validate_cancel_source_binding,
)
from bt_api_ctp.gateway_return_buffer import CtpGatewayReturnRecord


class CtpGatewayCancelReturnError(ValueError):
    """Finite, value-free rejection; raw callback data is never rendered."""

    def __init__(self, code: str) -> None:
        self.code = (
            code
            if code in {"UNSUPPORTED_RETURN", "INVALID_CTP_CANCEL_RETURN"}
            else "INVALID_CTP_CANCEL_RETURN"
        )
        super().__init__(self.code)


@dataclass(frozen=True, slots=True)
class CtpGatewayCancelErrorProjection:
    fact: GatewayReturnFact
    cancel_key: str
    prepared_digest: str
    source_binding: Mapping[str, str | int]


def _source(record: CtpGatewayReturnRecord) -> dict[str, str | int]:
    return validate_cancel_source_binding(
        {
            "schema": "ctp-cancel-source-v1",
            "source_ref": record.source_ref,
            "source_tags_digest": source_tags_digest(record.source_tags),
            "login_request_id": record.login_request_id,
        }
    )


def gateway_cancel_error_key(
    record: CtpGatewayReturnRecord, *, expected_broker: str, expected_investor: str
) -> str:
    """Derive a lookup key from a separately verified current callback source."""
    try:
        if (
            type(record) is not CtpGatewayReturnRecord
            or type(record.kind) is not str
            or type(record.sequence) is not int
            or not 0 < record.sequence < 2**63
        ):
            raise ValueError
        if record.kind not in {"action_response", "action_error"}:
            raise CtpGatewayCancelReturnError("UNSUPPORTED_RETURN")
        row = record.row
        if type(row) is not MappingProxyType:
            raise ValueError
        if (
            type(expected_broker) is not str
            or not expected_broker
            or expected_broker != expected_broker.strip()
            or type(expected_investor) is not str
            or not expected_investor
            or expected_investor != expected_investor.strip()
            or row.get("BrokerID") != expected_broker
            or type(row.get("BrokerID")) is not str
            or row.get("InvestorID") != expected_investor
            or type(row.get("InvestorID")) is not str
            or type(record.error_code) is not int
            or not 0 < record.error_code < 2**31
        ):
            raise ValueError
        request_id, action_ref = row.get("RequestID"), row.get("OrderActionRef")
        if (
            type(request_id) is not int
            or not 0 < request_id < 2**31
            or type(action_ref) is not int
            or not 0 < action_ref < 2**31
            or request_id != action_ref
        ):
            raise ValueError
        if record.kind == "action_response":
            if (
                type(record.request_id) is not int
                or record.request_id != request_id
                or type(record.is_last) is not bool
            ):
                raise ValueError
        elif record.request_id is not None or record.is_last is not None:
            raise ValueError
        return cancel_attempt_key(
            broker=expected_broker,
            investor=expected_investor,
            day=record.reception_trading_day,
            front=record.front_id,
            session=record.session_id,
            request_id=request_id,
            action_ref=action_ref,
            source_binding=_source(record),
        )
    except CtpGatewayCancelReturnError:
        raise
    except Exception:
        raise CtpGatewayCancelReturnError("INVALID_CTP_CANCEL_RETURN") from None


def project_gateway_cancel_error(
    record: CtpGatewayReturnRecord,
    prepared: PreparedCancelRequest,
    *,
    account_scope: str,
    expected_broker: str,
    expected_investor: str,
    instrument: MarketInstrumentKey,
) -> CtpGatewayCancelErrorProjection:
    """Validate a callback against an exact durable prepared cancel attempt."""
    try:
        key = gateway_cancel_error_key(
            record, expected_broker=expected_broker, expected_investor=expected_investor
        )
        if type(prepared) is not PreparedCancelRequest:
            raise ValueError
        if prepared.source_binding is None:
            raise CtpGatewayCancelReturnError("UNSUPPORTED_RETURN")
        prepared_digest = prepared.digest()
        source = validate_cancel_source_binding(prepared.source_binding)
        fields, row = prepared.request_fields, record.row
        if (
            source != _source(record)
            or prepared.request_keys != (key,)
            or prepared.trading_day != record.reception_trading_day
            or prepared.target_identity.fields.get("trading_day") != prepared.trading_day
            or prepared.target_identity.exchange_name != "CTP___FUTURE"
            or type(record.connection_generation) is not int
            or prepared.session_generation != record.connection_generation
            or type(fields.get("DispatchFrontID")) is not int
            or type(fields.get("DispatchSessionID")) is not int
            or fields.get("DispatchFrontID") != record.front_id
            or fields.get("DispatchSessionID") != record.session_id
            or type(instrument) is not MarketInstrumentKey
            or instrument.provider != "ctp"
            or instrument.product_type != "future"
            or instrument.venue != fields.get("ExchangeID")
            or instrument.native_symbol != prepared.symbol
            or instrument.instrument_id != prepared.symbol
        ):
            raise ValueError
        for name in (
            "BrokerID",
            "InvestorID",
            "InstrumentID",
            "ExchangeID",
            "ActionFlag",
            "RequestID",
            "OrderActionRef",
            "OrderRef",
            "FrontID",
            "SessionID",
            "OrderSysID",
        ):
            if (
                name not in fields
                or name not in row
                or type(row[name]) is not type(fields[name])
                or row[name] != fields[name]
            ):
                raise ValueError
        if (
            fields["BrokerID"] != expected_broker
            or fields["InvestorID"] != expected_investor
            or fields["InstrumentID"] != prepared.symbol
            or fields["ActionFlag"] != "0"
        ):
            raise ValueError
        target = prepared.target_identity
        target_fields = target.fields
        if (
            target_fields.get("exchange_id") != fields["ExchangeID"]
            or type(fields["OrderRef"]) is not str
            or type(fields["OrderSysID"]) is not str
            or type(fields["FrontID"]) is not int
            or type(fields["SessionID"]) is not int
        ):
            raise ValueError
        if fields["OrderRef"]:
            if (
                fields["OrderRef"] != (target_fields.get("order_ref") or target.client_order_id)
                or type(target_fields.get("front_id")) is not int
                or type(target_fields.get("session_id")) is not int
                or fields["FrontID"] != target_fields.get("front_id")
                or fields["SessionID"] != target_fields.get("session_id")
                or fields["OrderSysID"] != ""
            ):
                raise ValueError
        elif (
            fields["FrontID"] != 0
            or fields["SessionID"] != 0
            or not fields["OrderSysID"]
            or fields["OrderSysID"]
            != (target_fields.get("order_sys_id_raw") or target_fields.get("order_sys_id"))
            or fields["OrderSysID"].strip() != target_fields.get("order_sys_id")
        ):
            raise ValueError
        source_payload = {
            "cancel_key": key,
            "kind": record.kind,
            "error_code": record.error_code,
            "source_binding": source,
        }
        encoded = json.dumps(
            source_payload, sort_keys=True, ensure_ascii=True, separators=(",", ":")
        )
        fact = GatewayReturnFact(
            account_scope=account_scope,
            route="CTP___FUTURE",
            instrument=instrument,
            trading_day=prepared.trading_day,
            kind=DirectExecutionEventKind.CANCEL_ERROR,
            source_ref="ctp-cancel-error-v1:" + hashlib.sha256(encoded.encode("ascii")).hexdigest(),
            identity=prepared.target_identity,
            error_code=f"ctp_cancel_error_{record.error_code}",
            session_generation=record.connection_generation,
            schema_version=2,
            cancel_request_key=key,
        )
        return CtpGatewayCancelErrorProjection(fact, key, prepared_digest, MappingProxyType(source))
    except CtpGatewayCancelReturnError:
        raise
    except Exception:
        raise CtpGatewayCancelReturnError("INVALID_CTP_CANCEL_RETURN") from None
