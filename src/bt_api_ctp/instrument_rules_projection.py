"""CTP native instrument-rule projection."""


def project_instrument_rules(row, *, pick, normalized_error):
    NormalizedApiError = normalized_error
    filters = {
        item.get("filterType"): item for item in row.get("filters", []) if isinstance(item, dict)
    }
    lot = filters.get("LOT_SIZE") or filters.get("MARKET_LOT_SIZE") or {}
    price_filter = filters.get("PRICE_FILTER") or {}
    notional_filter = filters.get("MIN_NOTIONAL") or filters.get("NOTIONAL") or {}
    if row.get("metadata_complete") is not True or row.get("evidence_complete") is not True:
        raise NormalizedApiError("get_instrument_spec", "ctp_metadata_incomplete")
    required = {
        "contract_value": pick(row, "contract_value", "volume_multiple"),
        "contract_multiplier": pick(row, "contract_multiplier", default="1"),
        "price_tick": pick(row, "price_tick", "tick_size"),
        "quantity_step": pick(row, "quantity_step", default="1"),
        "min_quantity": pick(row, "min_quantity", default="1"),
    }
    max_quantity_raw = pick(row, "max_quantity")
    quantity_unit = "lots"
    min_notional_raw = pick(
        row, "min_notional", default=pick(notional_filter, "notional", "minNotional")
    )
    return (required, max_quantity_raw, quantity_unit, min_notional_raw, False)
