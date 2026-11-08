"""CTP insert-error projection following caller-verified callback provenance.

This describes one request error, never terminal order state or venue enqueue time.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from bt_api_base.direct_place_protocol import PreparedPlaceRequest
from bt_api_base.direct_write_protocol import DirectExecutionEventKind
from bt_api_base.gateway_execution_protocol import GatewayReturnFact
from bt_api_base.market_protocol import MarketInstrumentKey

from bt_api_ctp.gateway_cancel_source import source_tags_digest
from bt_api_ctp.gateway_place_source import place_attempt_key, validate_place_source_binding
from bt_api_ctp.gateway_return_buffer import CtpGatewayReturnRecord


class CtpGatewayPlaceReturnError(ValueError):
    """Finite value-free callback rejection."""

    def __init__(self, code: str) -> None:
        self.code = (
            code
            if code in {"UNSUPPORTED_RETURN", "INVALID_CTP_PLACE_RETURN"}
            else "INVALID_CTP_PLACE_RETURN"
        )
        super().__init__(self.code)


@dataclass(frozen=True, slots=True)
class CtpGatewayPlaceErrorProjection:
    fact: GatewayReturnFact
    place_key: str
    prepared_digest: str
    source_binding: Mapping[str, str | int]


def _source(record: CtpGatewayReturnRecord) -> dict[str, str | int]:
    return validate_place_source_binding(
        {
            "schema": "ctp-place-source-v1",
            "source_ref": record.source_ref,
            "source_tags_digest": source_tags_digest(record.source_tags),
            "login_request_id": record.login_request_id,
        }
    )


def gateway_place_error_key(
    record: CtpGatewayReturnRecord, *, expected_broker: str, expected_investor: str
) -> str:
    """Derive a lookup key after the caller checks exact current record identity."""
    try:
        if (
            type(record) is not CtpGatewayReturnRecord
            or type(record.kind) is not str
            or type(record.sequence) is not int
            or not 0 < record.sequence < 2**63
        ):
            raise ValueError
        if record.kind not in {"insert_response", "insert_error"}:
            raise CtpGatewayPlaceReturnError("UNSUPPORTED_RETURN")
        row = record.row
        if (
            type(row) is not MappingProxyType
            or type(expected_broker) is not str
            or not expected_broker
            or expected_broker != expected_broker.strip()
            or type(expected_investor) is not str
            or not expected_investor
            or expected_investor != expected_investor.strip()
            or type(row.get("BrokerID")) is not str
            or row["BrokerID"] != expected_broker
            or type(row.get("InvestorID")) is not str
            or row["InvestorID"] != expected_investor
            or type(record.error_code) is not int
            or not 0 < record.error_code < 2**31
        ):
            raise ValueError
        request_id, ref = row.get("RequestID"), row.get("OrderRef")
        if type(request_id) is not int or not 0 < request_id < 2**31:
            raise ValueError
        if record.kind == "insert_response":
            if (
                type(record.request_id) is not int
                or record.request_id != request_id
                or type(record.is_last) is not bool
            ):
                raise ValueError
        elif record.request_id is not None or record.is_last is not None:
            raise ValueError
        return place_attempt_key(
            broker=expected_broker,
            investor=expected_investor,
            day=record.reception_trading_day,
            front=record.front_id,
            session=record.session_id,
            request_id=request_id,
            order_ref=ref,
            source_binding=_source(record),
        )
    except CtpGatewayPlaceReturnError:
        raise
    except Exception:
        raise CtpGatewayPlaceReturnError("INVALID_CTP_PLACE_RETURN") from None


def project_gateway_place_error(
    record: CtpGatewayReturnRecord,
    prepared: PreparedPlaceRequest,
    *,
    account_scope: str,
    expected_broker: str,
    expected_investor: str,
    instrument: MarketInstrumentKey,
) -> CtpGatewayPlaceErrorProjection:
    """Project one exact prepared attempt's insert error, without order finality."""
    try:
        key = gateway_place_error_key(
            record, expected_broker=expected_broker, expected_investor=expected_investor
        )
        if type(prepared) is not PreparedPlaceRequest:
            raise ValueError
        digest = prepared.digest()
        source = validate_place_source_binding(prepared.source_binding)
        fields, row, identity = prepared.request_fields, record.row, prepared.identity
        if (
            source != _source(record)
            or prepared.request_keys != (key,)
            or prepared.trading_day != record.reception_trading_day
            or type(record.connection_generation) is not int
            or prepared.session_generation != record.connection_generation
            or identity.exchange_name != "CTP___FUTURE"
            or identity.session_generation != prepared.session_generation
            or identity.fields.get("trading_day") != prepared.trading_day
            or type(identity.fields.get("front_id")) is not int
            or identity.fields["front_id"] != record.front_id
            or type(identity.fields.get("session_id")) is not int
            or identity.fields["session_id"] != record.session_id
            or identity.fields.get("exchange_id") != fields.get("ExchangeID")
            or identity.client_order_id != fields.get("OrderRef")
            or identity.fields.get("order_ref") != fields.get("OrderRef")
            or type(instrument) is not MarketInstrumentKey
            or instrument.provider != "ctp"
            or instrument.product_type != "future"
            or instrument.venue != fields.get("ExchangeID")
            or instrument.native_symbol != prepared.symbol
            or instrument.instrument_id != prepared.symbol
        ):
            raise ValueError
        expected_names = {
            "BrokerID",
            "InvestorID",
            "UserID",
            "InstrumentID",
            "ExchangeID",
            "Direction",
            "CombOffsetFlag",
            "CombHedgeFlag",
            "VolumeTotalOriginal",
            "MinVolume",
            "ForceCloseReason",
            "IsAutoSuspend",
            "UserForceClose",
            "ContingentCondition",
            "OrderPriceType",
            "TimeCondition",
            "VolumeCondition",
            "LimitPrice",
            "OrderRef",
            "RequestID",
        }
        if set(fields) != expected_names:
            raise ValueError
        for name, expected in fields.items():
            if name not in row:
                raise ValueError
            actual = row[name]
            if name == "LimitPrice":
                if (
                    type(expected) is not str
                    or type(actual) is not float
                    or not math.isfinite(actual)
                    or actual <= 0
                    or actual.hex() != expected
                    or float.fromhex(expected).hex() != expected
                ):
                    raise ValueError
            elif type(actual) is not type(expected) or actual != expected:
                raise ValueError
        if (
            fields["BrokerID"] != expected_broker
            or fields["InvestorID"] != expected_investor
            or fields["InstrumentID"] != prepared.symbol
        ):
            raise ValueError
        payload = {
            "place_key": key,
            "kind": record.kind,
            "error_code": record.error_code,
            "source_binding": source,
        }
        encoded = json.dumps(payload, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
        fact = GatewayReturnFact(
            account_scope=account_scope,
            route="CTP___FUTURE",
            instrument=instrument,
            trading_day=prepared.trading_day,
            kind=DirectExecutionEventKind.ORDER_ERROR,
            source_ref="ctp-place-error-v1:" + hashlib.sha256(encoded.encode("ascii")).hexdigest(),
            identity=identity,
            error_code=f"ctp_place_error_{record.error_code}",
            session_generation=record.connection_generation,
            schema_version=3,
            place_request_key=key,
        )
        return CtpGatewayPlaceErrorProjection(fact, key, digest, MappingProxyType(source))
    except CtpGatewayPlaceReturnError:
        raise
    except Exception:
        raise CtpGatewayPlaceReturnError("INVALID_CTP_PLACE_RETURN") from None
