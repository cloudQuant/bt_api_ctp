"""CTP native order and cancel normalization choices."""

from types import MappingProxyType

from bt_api_base.normalization_policy import LegacyNormalizationPolicy


def _ctp_cancel_metadata(result, request):
    """Project only bounded native action identity/evidence into the SDK row."""
    getter = getattr(result, "get_extra_data", None)
    if not callable(getter):
        return {}
    try:
        extra = getter()
    except Exception:
        return {}
    if not isinstance(extra, dict):
        return {}
    envelope = extra.get("ctp_cancel")
    if not isinstance(envelope, dict):
        return {}
    evidence = envelope.get("evidence")
    if not isinstance(evidence, dict):
        as_dict = getattr(evidence, "as_dict", None)
        if callable(as_dict):
            try:
                evidence = as_dict()
            except Exception:
                evidence = None
    safe_fields = (
        "request_id",
        "order_action_ref",
        "status",
        "account_fingerprint",
        "trading_day",
        "connection_generation",
        "order_ref",
        "order_sys_id",
        "front_id",
        "session_id",
        "instrument_id",
        "exchange_id",
        "action_flag",
        "evidence_source",
        "evidence_received",
        "callback_received",
        "error_code",
        "reason",
        "observed_at_utc",
    )
    projected = (
        {key: evidence[key] for key in safe_fields if key in evidence}
        if isinstance(evidence, dict)
        else {}
    )
    # CtpRequestData exposes a redacted dictionary, not the immutable native
    # evidence object. Without the raw account binding this cannot be promoted
    # into matching callback evidence at the public normalization boundary.
    status = "unknown"
    if projected:
        projected["status"] = status
    return {
        "cancel_action_id": getattr(request, "idempotency_key", "") or "",
        "native_request_id": envelope.get("request_id"),
        "order_action_ref": envelope.get("order_action_ref"),
        "cancel_status": status,
        "cancel_evidence": projected,
    }


class CtpNormalizationPolicy(LegacyNormalizationPolicy):
    quantity_unit = "contracts"
    trade_authority = True
    balance_nested_fields = ("balances",)
    balance_fields = MappingProxyType({
        "currency": ("CurrencyID", "currency", "account_type", "symbol_name", "symbol"),
        "cash": (
            "available_cash", "available", "cash", "available_margin", "Available",
            "margin_free", "free_margin", "free",
        ),
        "value": ("Balance", "equity", "value"),
        "wallet": ("balance", "margin", "total_wallet_balance"),
        "unrealized": ("unrealized_profit",),
        "margin_used": ("margin_used", "used_margin", "CurrMargin"),
        "account_id": ("account_id", "AccountID"),
    })
    instrument_fields = MappingProxyType({
        "multiplier": ("multiplier", "contract_size", "VolumeMultiple"),
        "step": ("lot_size", "order_size_step", "volume_step"),
        "min_size": ("min_size", "volume_min", "MinLimitOrderVolume"),
        "max_size": ("max_size", "volume_max", "MaxLimitOrderVolume"),
        "tick_size": ("tick_size", "price_tick", "PriceTick"),
        "settlement_currency": ("settlement_currency", "currency"),
        "base_currency": ("base_currency",),
        "margin_rate": ("margin_rate", "LongMarginRatio", "long_margin_rate"),
        "commission_rate": ("commission_rate", "taker_commission_rate"),
        "exchange_id": ("exchange_id", "ExchangeID"),
    })
    instrument_uses_filters = False
    instrument_contract_value_field = None
    instrument_contract_multiplier_field = None
    instrument_linear_type_field = None
    instrument_perpetual_type_field = None
    event_id_fields = ("event_id", "trade_id", "fill_id", "order_id")
    event_audit_fields = ("sequence", "sequence_id", "previous_sequence")
    orderbook_sequence_fields = ("sequence", "sequence_id", "update_id")
    orderbook_previous_fields = ("previous_sequence", "prev_sequence")
    orderbook_delta_predecessor_fields = ()

    def order_average(self, row, average, *, pick, number):
        # CTP order reports contain limit price, not an execution VWAP.
        return number(pick(row, "execution_avg_price"))

    def apply_cancel_result(self, normalized, result, request):
        normalized.update(_ctp_cancel_metadata(result, request))
        # An action callback never proves the resting order's final state.
        normalized.update(
            status="submitted",
            execution_unknown=True,
            terminal_confirmed=False,
            definite_reject=False,
        )


def normalization_policy(exchange_name):
    return CtpNormalizationPolicy()


__all__ = ("normalization_policy",)
