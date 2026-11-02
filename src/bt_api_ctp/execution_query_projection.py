"""Neutral projection for source-sealed CTP execution query rows.

Only the CTP package reads native order, trade and rate fields. This module
projects facts into ``bt_api_base`` DTOs after the query service has validated
the sealed query source and current session. It never exposes native order
identities, CTP request IDs, or the account fingerprint.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from decimal import Decimal
from typing import Any, Callable

from bt_api_base.account_protocol import AccountOpenOrder, AccountResourceKey
from bt_api_base.execution_correlation_protocol import (
    ManagedOrderObservationIssuer,
    OrderCorrelation,
)
from bt_api_base.execution_query_protocol import (
    CommissionRateRecord,
    ExecutionOrderRecord,
    ExecutionTradeRecord,
    MarginRateRecord,
    NOTIONAL_RATE_UNIT_FRACTION_OF_NOTIONAL,
    RATE_AMOUNT_UNIT_CURRENCY_PER_CONTRACT,
)
from bt_api_base.market_protocol import MarketInstrumentKey
from bt_api_ctp.account_provider import (
    _ctp_datetime_utc,
    _decimal_text,
    _field,
    _integer,
    _utc_text,
)
from bt_api_ctp.registered_sim_session import CtpQuerySessionBinding

_OFFSET_EFFECTS = {
    "0": "open",
    "1": "close",
    "2": "close",
    "3": "close_today",
    "4": "close_yesterday",
    "5": "close",
    "6": "close",
}
_DIRECTION_SIDES = {"0": "buy", "1": "sell"}
_ORDER_STATUSES = {
    "0": "filled",
    "1": "partially_filled",
    "2": "partially_filled_terminal",
    "3": "open",
    "4": "terminal_unfilled",
    "5": "canceled",
    "b": "trigger_pending",
    "c": "triggered",
}
_ORDER_TYPES = {"1": "market", "2": "limit"}
_FEE_FIELDS = {
    "open": ("OpenRatioByMoney", "OpenRatioByVolume"),
    "close": ("CloseRatioByMoney", "CloseRatioByVolume"),
    "close_today": ("CloseTodayRatioByMoney", "CloseTodayRatioByVolume"),
}
_INVESTOR_RANGE = {"1": "all", "2": "group", "3": "single"}
_MISSING = object()
_PROVIDER_NAME = "CTP___FUTURE"
_ORDER_OBSERVATION_ID = re.compile(
    r"^sdk-order-observation-v1-[0-9a-f]{64}$", re.ASCII
)
_TRADE_OBSERVATION_ID = re.compile(
    r"^sdk-trade-observation-v1-[0-9a-f]{64}$", re.ASCII
)
_ACTIVE_ORDER_STATUSES = frozenset({"1", "3", "b", "c"})
_TERMINAL_ORDER_STATUSES = frozenset({"0", "2", "4", "5"})


class CtpExecutionQueryProjector:
    """Project CTP rows using the SDK's durable, scope-bound ID issuer.

    Provider-native identities are encoded only into transient private input
    strings for the injected issuer. The issuer owns stable HMAC IDs and any
    managed-order correlation binding; this projection never builds a local
    key or exposes a native identifier.
    """

    def __init__(
        self,
        session: CtpQuerySessionBinding,
        *,
        observation_issuer: ManagedOrderObservationIssuer | None,
        ledger_environment: str | None,
        account_ref: str | None,
        scope_ref: str | None,
        managed_order_resolver: Callable[
            [str, MarketInstrumentKey], tuple[OrderCorrelation, object] | None
        ]
        | None = None,
    ) -> None:
        if type(session) is not CtpQuerySessionBinding:
            raise TypeError("session must be a CtpQuerySessionBinding")
        if observation_issuer is not None and not isinstance(
            observation_issuer,
            ManagedOrderObservationIssuer,
        ):
            raise TypeError("observation_issuer must satisfy ManagedOrderObservationIssuer")
        for value in (ledger_environment, account_ref, scope_ref):
            if value is not None and (
                type(value) is not str or not value or value != value.strip()
            ):
                raise ValueError("observation scope must be exact nonempty text")
        if (observation_issuer is None) != (
            ledger_environment is None or account_ref is None
        ):
            raise ValueError("observation issuer and trusted scope must be supplied together")
        if managed_order_resolver is not None and not callable(managed_order_resolver):
            raise TypeError("managed_order_resolver must be callable")
        self._session = session
        self._observation_issuer = observation_issuer
        self._ledger_environment = ledger_environment
        self._account_ref = account_ref
        self._scope_ref = scope_ref
        self._managed_order_resolver = managed_order_resolver

    def project_orders(
        self,
        rows: tuple[Any, ...],
        *,
        observed_at: str,
        catalog_types: Mapping[tuple[str, str], str],
        filters: Mapping[str, Any],
    ) -> tuple[tuple[Mapping[str, Any], ...], tuple[str, ...]]:
        records: list[Mapping[str, Any]] = []
        blockers: list[str] = []
        seen: set[str] = set()
        for row in rows:
            try:
                instrument = self._instrument(row, catalog_types, filters)
                exchange_id = _required_text(_field(row, "ExchangeID"))
                trading_day = _required_text(_field(row, "TradingDay"))
                if trading_day != self._session.trading_day:
                    raise _ProjectionIssue("order_trading_day_mismatch")
                provider_identity = self._order_provider_identity(
                    row,
                    instrument,
                    exchange_id,
                    trading_day,
                )
                order_id = self._issue_order_id(provider_identity)
                if order_id in seen:
                    raise _ProjectionIssue("duplicate_order_identity")
                client_order_id, correlation_blocker = self._managed_client_order_id(
                    order_id,
                    provider_identity,
                    row,
                    instrument,
                )
                if correlation_blocker is not None:
                    blockers.append(correlation_blocker)

                side = _DIRECTION_SIDES.get(_required_text(_field(row, "Direction")))
                if side is None:
                    raise _ProjectionIssue("order_direction_unavailable")
                effect, position_side = _position_projection(
                    side, _optional_text(_field(row, "CombOffsetFlag"))
                )

                quantity = _integer(_field(row, "VolumeTotalOriginal"))
                filled = _integer(_field(row, "VolumeTraded"))
                if quantity <= 0 or filled < 0 or filled > quantity:
                    raise _ProjectionIssue("order_quantity_invalid")

                status_code = _optional_text(_field(row, "OrderStatus"))
                status = _ORDER_STATUSES.get(status_code or "", "unknown")
                if (status == "filled" and filled != quantity) or (
                    status in {"partially_filled", "partially_filled_terminal"}
                    and not 0 < filled < quantity
                ) or (status == "open" and filled != 0) or (
                    status == "terminal_unfilled" and filled != 0
                ):
                    raise _ProjectionIssue("order_status_quantity_conflict")

                price_type_code = _optional_text(_field(row, "OrderPriceType"))
                order_type = _ORDER_TYPES.get(price_type_code or "", "unknown")
                limit_price = (
                    _decimal_text(_field(row, "LimitPrice"))
                    if order_type == "limit"
                    else None
                )
                if limit_price is not None and _decimal_value(limit_price) <= 0:
                    raise _ProjectionIssue("order_limit_price_invalid")

                projected = ExecutionOrderRecord(
                    resource=_resource(instrument),
                    instrument=instrument,
                    order_id=order_id,
                    client_order_id=client_order_id,
                    intent_binding=("verified" if client_order_id is not None else "unresolved"),
                    side=side,
                    position_effect=effect,
                    position_side=position_side,
                    position_side_status=("verified" if position_side is not None else "unavailable"),
                    order_type=order_type,
                    status=status,
                    quantity=str(quantity),
                    filled_quantity=str(filled),
                    qty_unit="contract",
                    limit_price=limit_price,
                    price_currency=None,
                    price_currency_status="unavailable",
                    created_at_utc=None,
                    created_at_status="unavailable",
                    observed_at_utc=observed_at,
                )
                records.append(projected.to_dict())
                seen.add(order_id)
            except _ProjectionIssue as exc:
                blockers.append(exc.code)
            except Exception:
                blockers.append("order_record_invalid")
        return tuple(records), tuple(sorted(set(blockers)))

    def project_trades(
        self,
        rows: tuple[Any, ...],
        *,
        observed_at: str,
        catalog_types: Mapping[tuple[str, str], str],
        filters: Mapping[str, Any],
    ) -> tuple[tuple[Mapping[str, Any], ...], tuple[str, ...]]:
        records: list[Mapping[str, Any]] = []
        blockers: list[str] = []
        seen: set[str] = set()
        for row in rows:
            try:
                instrument = self._instrument(row, catalog_types, filters)
                exchange_id = _required_text(_field(row, "ExchangeID"))
                trading_day = _required_text(_field(row, "TradingDay"))
                if trading_day != self._session.trading_day:
                    raise _ProjectionIssue("trade_trading_day_mismatch")
                native_trade_id = _required_text(_field(row, "TradeID"))
                trade_identity = _private_identity(
                    "trade",
                    exchange_id=exchange_id,
                    trading_day=trading_day,
                    instrument_id=instrument.instrument_id,
                    trade_id=native_trade_id,
                )
                trade_id = self._issue_trade_id(trade_identity)
                if trade_id in seen:
                    raise _ProjectionIssue("duplicate_trade_identity")

                side = _DIRECTION_SIDES.get(_required_text(_field(row, "Direction")))
                if side is None:
                    raise _ProjectionIssue("trade_direction_unavailable")
                effect, position_side = _position_projection(
                    side, _optional_text(_field(row, "OffsetFlag"))
                )
                quantity = _integer(_field(row, "Volume"))
                if quantity <= 0:
                    raise _ProjectionIssue("trade_quantity_invalid")
                price = _decimal_text(_field(row, "Price"))
                if _decimal_value(price) <= 0:
                    raise _ProjectionIssue("trade_price_invalid")

                order_id = self._optional_order_id(
                    row,
                    instrument,
                    exchange_id,
                    trading_day,
                )
                projected = ExecutionTradeRecord(
                    resource=_resource(instrument),
                    instrument=instrument,
                    trade_id=trade_id,
                    order_id=order_id,
                    order_binding=(
                        "provider_order_identity" if order_id is not None else "unresolved"
                    ),
                    side=side,
                    position_effect=effect,
                    position_side=position_side,
                    position_side_status=("verified" if position_side is not None else "unavailable"),
                    quantity=str(quantity),
                    qty_unit="contract",
                    price=price,
                    price_currency=None,
                    price_currency_status="unavailable",
                    traded_at_utc=None,
                    traded_at_status="unavailable",
                    observed_at_utc=observed_at,
                )
                records.append(projected.to_dict())
                seen.add(trade_id)
            except _ProjectionIssue as exc:
                blockers.append(exc.code)
            except Exception:
                blockers.append("trade_record_invalid")
        return tuple(records), tuple(sorted(set(blockers)))

    def project_open_orders(
        self,
        rows: tuple[Any, ...],
        *,
        observed_at: str,
        catalog_types: Mapping[tuple[str, str], str],
        filters: Mapping[str, Any],
    ) -> tuple[tuple[Mapping[str, Any], ...], tuple[str, ...]]:
        """Project the complete query result to neutral open-order records.

        Known terminal order states are omitted. Unknown states block a
        complete projection because they might represent active orders. A
        missing managed correlation leaves the provider observation visible
        with ``client_order_id=None``; the opaque observation token is never a
        cancel authorization.
        """

        records: list[Mapping[str, Any]] = []
        blockers: list[str] = []
        seen: set[str] = set()
        requested_order_id = filters.get("provider_order_id")
        if requested_order_id is not None and (
            type(requested_order_id) is not str
            or not _ORDER_OBSERVATION_ID.fullmatch(requested_order_id)
        ):
            return (), ("provider_order_id_invalid",)

        for row in rows:
            try:
                status_code = _optional_text(_field(row, "OrderStatus"))
                if status_code in _TERMINAL_ORDER_STATUSES:
                    continue
                if status_code not in _ACTIVE_ORDER_STATUSES:
                    raise _ProjectionIssue("open_order_status_unrecognized")

                instrument = self._instrument(row, catalog_types, filters)
                exchange_id = _required_text(_field(row, "ExchangeID"))
                trading_day = _required_text(_field(row, "TradingDay"))
                if trading_day != self._session.trading_day:
                    raise _ProjectionIssue("open_order_trading_day_mismatch")
                provider_identity = self._order_provider_identity(
                    row,
                    instrument,
                    exchange_id,
                    trading_day,
                )
                order_id = self._issue_order_id(provider_identity)
                if order_id in seen:
                    raise _ProjectionIssue("duplicate_order_identity")
                if requested_order_id is not None and order_id != requested_order_id:
                    seen.add(order_id)
                    continue

                side = _DIRECTION_SIDES.get(_required_text(_field(row, "Direction")))
                if side is None:
                    raise _ProjectionIssue("open_order_direction_unavailable")
                effect, position_side = _position_projection(
                    side,
                    _optional_text(_field(row, "CombOffsetFlag")),
                )
                if position_side is None:
                    raise _ProjectionIssue("open_order_position_side_unavailable")

                quantity = _integer(_field(row, "VolumeTotalOriginal"))
                filled = _integer(_field(row, "VolumeTraded"))
                if quantity <= 0 or filled < 0 or filled >= quantity:
                    raise _ProjectionIssue("open_order_quantity_invalid")

                order_type_code = _required_text(_field(row, "OrderPriceType"))
                order_type = _ORDER_TYPES.get(order_type_code)
                if order_type not in {"market", "limit"}:
                    raise _ProjectionIssue("open_order_type_unavailable")
                limit_price = (
                    _decimal_text(_field(row, "LimitPrice"))
                    if order_type == "limit"
                    else None
                )
                if limit_price is not None and _decimal_value(limit_price) <= 0:
                    raise _ProjectionIssue("open_order_limit_price_invalid")

                client_order_id, correlation_blocker = self._managed_client_order_id(
                    order_id,
                    provider_identity,
                    row,
                    instrument,
                )
                if correlation_blocker is not None:
                    blockers.append(correlation_blocker)
                record = AccountOpenOrder(
                    resource=_resource(instrument),
                    instrument=instrument,
                    order_id=order_id,
                    side=side,
                    position_side=position_side,
                    order_type=order_type,
                    status="partially_filled" if filled else "open",
                    quantity=str(quantity),
                    filled_quantity=str(filled),
                    qty_unit="contract",
                    created_at_utc=_ctp_datetime_utc(
                        _field(row, "InsertDate"),
                        _field(row, "InsertTime"),
                    ),
                    observed_at_utc=observed_at,
                    client_order_id=client_order_id,
                    limit_price=limit_price,
                    stop_price=None,
                    price_currency=None,
                    reduce_only=None,
                )
                records.append(record.to_dict())
                seen.add(order_id)
                if len(records) > 2048:
                    raise _ProjectionIssue("open_order_record_limit_exceeded")
            except _ProjectionIssue as exc:
                blockers.append(exc.code)
            except Exception:
                blockers.append("open_order_record_invalid")
        return tuple(records), tuple(sorted(set(blockers)))

    def project_commission_rates(
        self,
        rows: tuple[Any, ...],
        *,
        catalog_types: Mapping[tuple[str, str], str],
        filters: Mapping[str, Any],
    ) -> tuple[tuple[Mapping[str, Any], ...], tuple[str, ...]]:
        return self._project_rate_rows(
            rows,
            catalog_types=catalog_types,
            filters=filters,
            family="commission_rates",
        )

    def project_margin_rates(
        self,
        rows: tuple[Any, ...],
        *,
        catalog_types: Mapping[tuple[str, str], str],
        filters: Mapping[str, Any],
    ) -> tuple[tuple[Mapping[str, Any], ...], tuple[str, ...]]:
        return self._project_rate_rows(
            rows,
            catalog_types=catalog_types,
            filters=filters,
            family="margin_rates",
        )

    def _project_rate_rows(
        self,
        rows: tuple[Any, ...],
        *,
        catalog_types: Mapping[tuple[str, str], str],
        filters: Mapping[str, Any],
        family: str,
    ) -> tuple[tuple[Mapping[str, Any], ...], tuple[str, ...]]:
        records: list[Mapping[str, Any]] = []
        blockers: list[str] = []
        seen: set[tuple[Any, ...]] = set()
        if not rows:
            return (), ("rate_schedule_rows_empty",)
        for row in rows:
            try:
                instrument = self._instrument(row, catalog_types, filters)
                if instrument.product_type != "future":
                    raise _ProjectionIssue("rate_schedule_product_type_unsupported")
                scope = _schedule_scope(row, self._session)
                if scope == "unresolved":
                    blockers.append("rate_schedule_scope_unresolved")
                # Both CTP ratio fields lack an authoritative currency
                # dimension. Preserve the numeric provider facts, but do not
                # let a query receipt present them as decision-ready complete
                # commission or margin evidence.
                blockers.append("rate_currency_unavailable")
                _require_unscoped_invest_unit(row)
                if family == "commission_rates":
                    biz_type = _required_text(_field(row, "BizType"))
                    if biz_type != _native_future_business_type():
                        raise _ProjectionIssue("commission_business_type_unsupported")
                    for role, (money_field, volume_field) in _FEE_FIELDS.items():
                        notional, amount = _rate_dimensions(row, money_field, volume_field)
                        identity = (instrument, role, scope)
                        if identity in seen:
                            raise _ProjectionIssue("duplicate_commission_schedule_scope")
                        record = CommissionRateRecord(
                            resource=_resource(instrument),
                            instrument=instrument,
                            fee_role=role,
                            notional_rate=notional,
                            notional_rate_unit=(
                                NOTIONAL_RATE_UNIT_FRACTION_OF_NOTIONAL
                                if notional is not None
                                else None
                            ),
                            amount_per_contract=amount,
                            amount_unit=(
                                RATE_AMOUNT_UNIT_CURRENCY_PER_CONTRACT
                                if amount is not None
                                else None
                            ),
                            currency=None,
                            currency_status="unavailable",
                            schedule_scope=scope,
                        )
                        records.append(record.to_dict())
                        seen.add(identity)
                else:
                    hedge_class = filters.get("hedge_class")
                    if type(hedge_class) is not str:
                        raise _ProjectionIssue("margin_hedge_class_unavailable")
                    hedge_flag = native_hedge_flag(hedge_class)
                    row_hedge_flag = _required_text(_field(row, "HedgeFlag"))
                    if hedge_flag is None or row_hedge_flag != hedge_flag:
                        raise _ProjectionIssue("margin_hedge_class_mismatch")
                    dimensions = (
                        ("long", "LongMarginRatioByMoney", "LongMarginRatioByVolume"),
                        ("short", "ShortMarginRatioByMoney", "ShortMarginRatioByVolume"),
                    )
                    for side, money_field, volume_field in dimensions:
                        notional, amount = _rate_dimensions(row, money_field, volume_field)
                        identity = (instrument, hedge_class, side, scope)
                        if identity in seen:
                            raise _ProjectionIssue("duplicate_margin_schedule_scope")
                        record = MarginRateRecord(
                            resource=_resource(instrument),
                            instrument=instrument,
                            hedge_class=hedge_class,
                            position_side=side,
                            notional_rate=notional,
                            notional_rate_unit=(
                                NOTIONAL_RATE_UNIT_FRACTION_OF_NOTIONAL
                                if notional is not None
                                else None
                            ),
                            amount_per_contract=amount,
                            amount_unit=(
                                RATE_AMOUNT_UNIT_CURRENCY_PER_CONTRACT
                                if amount is not None
                                else None
                            ),
                            currency=None,
                            currency_status="unavailable",
                            schedule_scope=scope,
                        )
                        records.append(record.to_dict())
                        seen.add(identity)
            except _ProjectionIssue as exc:
                blockers.append(exc.code)
            except Exception:
                blockers.append("rate_record_invalid")
        return tuple(records), tuple(sorted(set(blockers)))

    def _instrument(
        self,
        row: Any,
        catalog_types: Mapping[tuple[str, str], str],
        filters: Mapping[str, Any],
    ) -> MarketInstrumentKey:
        instrument_id = _required_text(_field(row, "InstrumentID"))
        exchange_id = _required_text(_field(row, "ExchangeID"))
        requested_id = filters.get("instrument_id", filters.get("symbol"))
        requested_exchange = filters.get("exchange")
        if requested_id is not None and requested_id != instrument_id:
            raise _ProjectionIssue("instrument_filter_mismatch")
        if requested_exchange is not None and requested_exchange != exchange_id:
            raise _ProjectionIssue("exchange_filter_mismatch")
        product_type = catalog_types.get((exchange_id, instrument_id))
        environment = self._session.environment
        if product_type not in {"future", "option"} or type(environment) is not str or not environment:
            raise _ProjectionIssue("instrument_reference_unavailable")
        return MarketInstrumentKey(
            provider="ctp",
            venue=exchange_id,
            environment=environment,
            product_type=product_type,
            instrument_id=instrument_id,
            native_symbol=instrument_id,
        )

    def _order_provider_identity(
        self,
        row: Any,
        instrument: MarketInstrumentKey,
        exchange_id: str,
        trading_day: str,
    ) -> str:
        system_id = _optional_text(_field(row, "OrderSysID"))
        if system_id is not None:
            return _private_identity(
                "order",
                identity_kind="system",
                exchange_id=exchange_id,
                trading_day=trading_day,
                instrument_id=instrument.instrument_id,
                order_system_id=system_id,
            )
        local_id = _optional_text(_field(row, "OrderLocalID"))
        try:
            front_id = _integer(_field(row, "FrontID"))
            session_id = _integer(_field(row, "SessionID"))
        except Exception:
            front_id = session_id = 0
        if local_id is not None and front_id > 0 and session_id > 0:
            return _private_identity(
                "order",
                identity_kind="local",
                exchange_id=exchange_id,
                trading_day=trading_day,
                instrument_id=instrument.instrument_id,
                front_id=front_id,
                session_id=session_id,
                order_local_id=local_id,
            )
        order_ref = _optional_text(_field(row, "OrderRef"))
        if order_ref is None or front_id <= 0 or session_id <= 0:
            raise _ProjectionIssue("order_identity_unavailable")
        return _private_identity(
            "order",
            identity_kind="front",
            exchange_id=exchange_id,
            trading_day=trading_day,
            instrument_id=instrument.instrument_id,
            front_id=front_id,
            session_id=session_id,
            order_ref=order_ref,
        )

    def _optional_order_id(
        self,
        row: Any,
        instrument: MarketInstrumentKey,
        exchange_id: str,
        trading_day: str,
    ) -> str | None:
        try:
            provider_identity = self._order_provider_identity(
                row,
                instrument,
                exchange_id,
                trading_day,
            )
        except _ProjectionIssue:
            # A trade remains a visible execution even when its order identity
            # is incomplete or cannot be safely issued. Absence of a supported
            # key never becomes a guess; the trade DTO carries unresolved.
            return None
        return self._issue_order_id(provider_identity)

    def _issue_order_id(self, provider_identity: str) -> str:
        issuer = self._observation_issuer
        environment = self._ledger_environment
        account_ref = self._account_ref
        if issuer is None or environment is None or account_ref is None:
            raise _ProjectionIssue("execution_observation_issuer_unavailable")
        try:
            order_id = issuer.issue_order_observation_id(
                _PROVIDER_NAME,
                environment,
                account_ref,
                provider_identity,
            )
        except Exception:
            raise _ProjectionIssue("execution_order_identity_unavailable") from None
        if type(order_id) is not str or not _ORDER_OBSERVATION_ID.fullmatch(order_id):
            raise _ProjectionIssue("execution_order_identity_untrusted")
        return order_id

    def _issue_trade_id(self, provider_identity: str) -> str:
        issuer = self._observation_issuer
        environment = self._ledger_environment
        account_ref = self._account_ref
        if issuer is None or environment is None or account_ref is None:
            raise _ProjectionIssue("execution_observation_issuer_unavailable")
        try:
            trade_id = issuer.issue_trade_observation_id(
                _PROVIDER_NAME,
                environment,
                account_ref,
                provider_identity,
            )
        except Exception:
            raise _ProjectionIssue("execution_trade_identity_unavailable") from None
        if type(trade_id) is not str or not _TRADE_OBSERVATION_ID.fullmatch(trade_id):
            raise _ProjectionIssue("execution_trade_identity_untrusted")
        return trade_id

    def _managed_client_order_id(
        self,
        order_id: str,
        provider_identity: str,
        row: Any,
        instrument: MarketInstrumentKey,
    ) -> tuple[str | None, str | None]:
        resolver = self._managed_order_resolver
        issuer = self._observation_issuer
        environment = self._ledger_environment
        account_ref = self._account_ref
        scope_ref = self._scope_ref
        if (
            resolver is None
            or issuer is None
            or environment is None
            or account_ref is None
            or scope_ref is None
        ):
            return None, None
        order_ref = _optional_text(_field(row, "OrderRef"))
        if order_ref is None:
            return None, None
        try:
            resolved = resolver(order_ref, instrument)
        except Exception:
            return None, "managed_order_correlation_unavailable"
        if resolved is None:
            return None, None
        if type(resolved) is not tuple or len(resolved) != 2:
            return None, "managed_order_correlation_invalid"
        correlation, reservation = resolved
        if (
            type(correlation) is not OrderCorrelation
            or correlation.exchange_name != _PROVIDER_NAME
            or correlation.symbol != instrument.instrument_id
            or reservation is None
            or type(reservation) in (bool, str, bytes, int, float)
        ):
            return None, "managed_order_correlation_invalid"
        try:
            valid_reservation = issuer.validate_order_correlation(
                correlation,
                issuer_reservation=reservation,
            )
        except Exception:
            valid_reservation = False
        if type(valid_reservation) is not bool or valid_reservation is not True:
            return None, "managed_order_correlation_invalid"
        try:
            bound = issuer.bind_order_observation(
                order_id,
                _PROVIDER_NAME,
                environment,
                account_ref,
                scope_ref,
                provider_identity,
                correlation,
                issuer_reservation=reservation,
            )
            observed = issuer.resolve_order_observation(
                order_id,
                provider_name=_PROVIDER_NAME,
                environment=environment,
                account_ref=account_ref,
                scope_ref=scope_ref,
            )
        except Exception:
            return None, "managed_order_correlation_unavailable"
        if (
            type(bound) is not bool
            or bound is not True
            or type(observed) is not tuple
            or len(observed) != 2
            or type(observed[0]) is not OrderCorrelation
            or observed[0] != correlation
        ):
            return None, "managed_order_correlation_unavailable"
        try:
            observed_reservation_valid = issuer.validate_order_correlation(
                observed[0],
                issuer_reservation=observed[1],
            )
        except Exception:
            observed_reservation_valid = False
        if (
            type(observed_reservation_valid) is not bool
            or observed_reservation_valid is not True
        ):
            return None, "managed_order_correlation_unavailable"
        return correlation.client_order_id, None


def native_hedge_flag(hedge_class: str) -> str | None:
    """Map a neutral hedge class to an exact CTP filter value, if available."""

    names = {
        "speculation": "THOST_FTDC_HF_Speculation",
        "arbitrage": "THOST_FTDC_HF_Arbitrage",
        "hedge": "THOST_FTDC_HF_Hedge",
    }
    name = names.get(hedge_class)
    if name is None:
        return None
    try:
        from bt_api_ctp.ctp import ctp_constants

        value = getattr(ctp_constants, name)
    except Exception:
        return None
    return value if type(value) is str and value and value == value.strip() else None


class _ProjectionIssue(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _required_text(value: Any) -> str:
    result = _ctp_text(value)
    if not result:
        raise ValueError("required CTP text is empty")
    return result


def _optional_text(value: Any) -> str | None:
    if value is None or value == "" or value == b"":
        return None
    if value == "\x00" or value == b"\x00":
        return None
    return _ctp_text(value)


def _ctp_text(value: Any) -> str:
    if type(value) is bytes:
        value = value.decode("gbk", errors="strict")
    if type(value) is not str:
        raise TypeError("CTP text field must be text")
    result = value.strip()
    if (
        not result
        or not result.isprintable()
        or len(result.encode("utf-8", errors="strict")) > 256
    ):
        raise ValueError("CTP text field is invalid")
    return result


def _position_projection(side: str, offset: str | None) -> tuple[str, str | None]:
    effect = _OFFSET_EFFECTS.get(offset or "")
    if effect is None:
        return "unavailable", None
    position_side = ("long" if side == "buy" else "short") if effect == "open" else (
        "short" if side == "buy" else "long"
    )
    return effect, position_side


def _resource(instrument: MarketInstrumentKey) -> AccountResourceKey:
    pool = {"future": "futures", "option": "options"}.get(instrument.product_type)
    if pool is None:
        raise ValueError("instrument resource is unsupported")
    return AccountResourceKey(product_type=instrument.product_type, margin_pool=pool)


def _private_identity(kind: str, **fields: Any) -> str:
    """Build a bounded transient identity string for the SDK-owned issuer."""

    payload = {"schema": f"bt_api_ctp.{kind}_observation_identity.v1", **fields}
    try:
        return json.dumps(
            payload,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError):
        raise _ProjectionIssue("provider_identity_invalid") from None


def _decimal_value(value: str) -> Decimal:
    return Decimal(value)


def _rate_dimensions(row: Any, notional_field: str, amount_field: str) -> tuple[str | None, str | None]:
    notional = _optional_rate(row, notional_field)
    amount = _optional_rate(row, amount_field)
    if notional is None and amount is None:
        raise _ProjectionIssue("rate_dimensions_unavailable")
    return notional, amount


def _optional_rate(row: Any, field_name: str) -> str | None:
    value = _field(row, field_name)
    if value is None:
        return None
    rendered = _decimal_text(value)
    if Decimal(rendered) < 0:
        raise ValueError("CTP rate must not be negative")
    return rendered


def _schedule_scope(row: Any, session: CtpQuerySessionBinding) -> str:
    broker = _required_text(_field(row, "BrokerID"))
    instrument = _required_text(_field(row, "InstrumentID"))
    exchange = _required_text(_field(row, "ExchangeID"))
    if broker != session.broker_id:
        raise _ProjectionIssue("rate_broker_identity_mismatch")
    if not instrument or not exchange:
        raise _ProjectionIssue("rate_instrument_identity_unavailable")
    range_code = _optional_text(_field(row, "InvestorRange"))
    investor = _optional_text(_field(row, "InvestorID"))
    range_name = _INVESTOR_RANGE.get(range_code or "")
    if range_name == "all":
        if investor not in {None, session.investor_id}:
            raise _ProjectionIssue("rate_investor_identity_mismatch")
        return "broker_default"
    if range_name == "single":
        if investor != session.investor_id:
            raise _ProjectionIssue("rate_investor_identity_mismatch")
        return "account_override"
    # Group membership and unknown range codes are not established by the
    # session query. Preserve the row as visible schedule data, but label its
    # applicability unresolved so consumers cannot treat it as account proof.
    return "unresolved"


def _require_unscoped_invest_unit(row: Any) -> None:
    value = _field_if_present(row, "InvestUnitID")
    if value is _MISSING:
        raise _ProjectionIssue("invest_unit_scope_unavailable")
    normalized = _optional_text(value)
    if normalized is not None:
        raise _ProjectionIssue("invest_unit_scope_unsupported")


def _field_if_present(row: Any, name: str) -> Any:
    try:
        return _field(row, name)
    except (AttributeError, KeyError, TypeError, ValueError):
        return _MISSING


def _native_future_business_type() -> str | None:
    try:
        from bt_api_ctp.ctp import ctp_constants

        value = ctp_constants.THOST_FTDC_BZTP_Future
    except Exception:
        return None
    return value if type(value) is str and value == value.strip() else None


__all__ = ["CtpExecutionQueryProjector", "native_hedge_flag"]
