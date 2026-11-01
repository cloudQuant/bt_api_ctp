"""CTP-native quote V2 field projection with SDK-owned proof callbacks."""

from __future__ import annotations


def build_quote_v2_projector(*, pick, number, trusted_text, parent_attestation_matches):
    """Bind neutral SDK helpers and the parent-owned attestation validator."""
    _ctp_quote_v2_trusted_text = trusted_text
    _ctp_quote_v2_parent_attestation_matches = parent_attestation_matches

    _CTP_QUOTE_V2_CONTINUITY = frozenset(
        {
            "continuous",
            "snapshot",
            "duplicate",
            "out_of_order",
            "gap",
            "checksum_failed",
            "unverified",
        }
    )

    def _ctp_quote_v2_number(row, *names, positive=False):
        try:
            value = number(pick(row, *names, default=None))
        except (TypeError, ValueError):
            return None
        if value is None or (positive and value <= 0) or (not positive and value < 0):
            return None
        return value

    def _ctp_quote_v2_nonnegative_int(row, *names) -> int:
        value = pick(row, *names, default=0)
        try:
            result = int(value)
        except (TypeError, ValueError):
            return 0
        return result if result >= 0 else 0

    def _ctp_quote_v2_text(row, *names, default="") -> str:
        value = pick(row, *names, default=default)
        return str(value or "").strip()

    def _ctp_quote_v2_asset_type(value) -> str:
        text = str(value or "").strip().lower()
        if text in {"future", "futures", "future_contract", "fut"}:
            return "future"
        if text in {"option", "options", "option_contract", "opt"}:
            return "option"
        return "unknown"

    def _ctp_quote_v2_quality_flags(row) -> tuple[str, ...]:
        value = row.get("quality_flags", ())
        if not isinstance(value, (list, tuple, set, frozenset)):
            return ("INVALID_QUALITY_FLAGS",)
        return tuple(str(flag) for flag in value)

    def _ctp_quote_v2_day_is_valid(value: str) -> bool:
        return len(value) == 8 and value.isdigit()

    def _ctp_quote_v2_fields(
        row,
        timestamps,
        *,
        exchange_name="",
        symbol="",
        parent_attestation=None,
    ):
        """Normalize quote V2 evidence without turning unknown evidence into trust.

        The gateway is expected to have checked the native CTP quote.  This second
        boundary deliberately rechecks every fact used by an execution decision,
        so a hand-built gateway/ZMQ payload cannot promote itself to eligible.
        """

        bid = _ctp_quote_v2_number(row, "bid_price", "bid", "BidPrice1", positive=True)
        ask = _ctp_quote_v2_number(row, "ask_price", "ask", "AskPrice1", positive=True)
        last = _ctp_quote_v2_number(row, "last_price", "price", "last", "LastPrice", positive=True)
        bid_size = _ctp_quote_v2_number(row, "bid_volume", "bid_size", "BidVolume1")
        ask_size = _ctp_quote_v2_number(row, "ask_volume", "ask_size", "AskVolume1")
        lower_limit = _ctp_quote_v2_number(
            row, "lower_limit_price", "lower_limit", "LowerLimitPrice", positive=True
        )
        upper_limit = _ctp_quote_v2_number(
            row, "upper_limit_price", "upper_limit", "UpperLimitPrice", positive=True
        )
        source_error = _ctp_quote_v2_number(row, "source_clock_error_ms")
        receive_error = _ctp_quote_v2_number(row, "receive_clock_error_ms")
        cumulative_volume = _ctp_quote_v2_number(row, "cum_volume", "cumulative_volume", "Volume")
        delta_volume = _ctp_quote_v2_number(row, "delta_volume", "volume")
        volume_semantics = _ctp_quote_v2_text(row, "volume_semantics").lower()
        volume_complete = pick(row, "volume_complete", default=False) is True
        volume_quality = _ctp_quote_v2_text(row, "volume_quality", default="unknown").upper()
        continuity_status = _ctp_quote_v2_text(row, "continuity_status", default="gap").lower()
        if continuity_status not in _CTP_QUOTE_V2_CONTINUITY:
            continuity_status = "gap"
        quality_flags = _ctp_quote_v2_quality_flags(row)
        trading_day = _ctp_quote_v2_text(row, "trading_day", "TradingDay")
        action_day = _ctp_quote_v2_text(row, "action_day", "ActionDay")
        event_time_source = _ctp_quote_v2_text(row, "event_time_source", default="unresolved").lower()
        source_clock_quality = _ctp_quote_v2_text(
            row, "source_clock_quality", default="unknown"
        ).lower()
        receive_clock_quality = _ctp_quote_v2_text(
            row, "receive_clock_quality", default="unknown"
        ).lower()
        source = _ctp_quote_v2_text(row, "source", default="unknown") or "unknown"
        rules_hash = _ctp_quote_v2_text(row, "rules_hash")
        clock_domain_id = _ctp_quote_v2_text(row, "clock_domain_id")
        # Do not infer futures from a legacy ``CTP___FUTURE`` venue suffix: one
        # CTP market stream can carry future and option legs together.
        asset_type = _ctp_quote_v2_asset_type(pick(row, "asset_type", "market_type", default=""))
        # Product identity is additive diagnostic evidence from the native CTP
        # adapter.  It is intentionally not eligible to self-attest a payload;
        # execution eligibility below still requires the sealed parent proof.
        product_class = _ctp_quote_v2_text(row, "product_class", "ProductClass") or None
        contract_type = (
            _ctp_quote_v2_text(row, "contract_type", "ContractType", default="unknown") or "unknown"
        )
        option_type = _ctp_quote_v2_text(row, "option_type", "OptionsType") or None
        underlying_instrument = (
            _ctp_quote_v2_text(row, "underlying_instrument", "UnderlyingInstrID") or None
        )
        strike_price = _ctp_quote_v2_number(row, "strike_price", "StrikePrice", positive=True)
        connection_generation = _ctp_quote_v2_nonnegative_int(row, "connection_generation")
        ingest_seq = _ctp_quote_v2_nonnegative_int(row, "ingest_seq")
        subscription_epoch = _ctp_quote_v2_nonnegative_int(row, "subscription_epoch")
        event_time = timestamps["exchange_time"]
        receive_time = timestamps["received_wall_time"]
        timing_causal = (
            event_time is not None
            and receive_time is not None
            and source_error is not None
            and receive_error is not None
            and event_time <= receive_time + (source_error + receive_error) / 1000
        )
        evidence_complete = (
            asset_type in {"future", "option"}
            and volume_semantics == "delta"
            and volume_complete
            and volume_quality == "CONTINUOUS"
            and continuity_status == "continuous"
            and not quality_flags
            and bid is not None
            and ask is not None
            and last is not None
            and bid_size is not None
            and ask_size is not None
            and bid_size > 0
            and ask_size > 0
            and bid <= ask
            and lower_limit is not None
            and upper_limit is not None
            and lower_limit < upper_limit
            and all(lower_limit <= price <= upper_limit for price in (bid, ask, last))
            and _ctp_quote_v2_day_is_valid(trading_day)
            and _ctp_quote_v2_day_is_valid(action_day)
            and event_time_source == "action_day"
            and bool(_ctp_quote_v2_trusted_text(rules_hash))
            and bool(_ctp_quote_v2_trusted_text(clock_domain_id))
            and bool(_ctp_quote_v2_trusted_text(source))
            and connection_generation > 0
            and ingest_seq > 0
            and subscription_epoch > 0
            and source_clock_quality == "verified"
            and receive_clock_quality == "verified"
            and pick(row, "freshness_verified", default=False) is True
            and timestamps["stale"] is False
            and timing_causal
        )
        parent_attested = _ctp_quote_v2_parent_attestation_matches(
            parent_attestation,
            exchange_name=exchange_name,
            symbol=symbol,
            source=source,
            rules_hash=rules_hash,
            clock_domain_id=clock_domain_id,
            connection_generation=connection_generation,
            subscription_epoch=subscription_epoch,
        )
        execution_eligible = evidence_complete and parent_attested
        result = {
            "asset_type": asset_type,
            "product_class": product_class,
            "contract_type": contract_type,
            "option_type": option_type,
            "underlying_instrument": underlying_instrument,
            "strike_price": strike_price,
            "price": last,
            "last_price": last,
            "bid_price": bid,
            "ask_price": ask,
            "bid_volume": bid_size,
            "ask_volume": ask_size,
            "schema_version": "ctp.quote.v2",
            "volume_semantics": volume_semantics,
            "volume": delta_volume if volume_semantics == "delta" else None,
            "cum_volume": cumulative_volume,
            "cumulative_volume": cumulative_volume,
            "delta_volume": delta_volume,
            "volume_complete": volume_complete,
            "volume_quality": volume_quality or "UNKNOWN",
            "continuity_status": continuity_status,
            "quality_flags": quality_flags,
            "lower_limit_price": lower_limit,
            "upper_limit_price": upper_limit,
            "trading_day": trading_day,
            "action_day": action_day,
            "event_time_utc": event_time,
            "recv_time_utc": receive_time,
            "recv_monotonic_ns": _ctp_quote_v2_nonnegative_int(
                row, "recv_monotonic_ns", "received_monotonic_ns"
            ),
            "connection_generation": connection_generation,
            "ingest_seq": ingest_seq,
            "subscription_epoch": subscription_epoch,
            "rules_hash": rules_hash,
            "clock_domain_id": clock_domain_id,
            "source": source,
            "event_time_source": event_time_source or "unresolved",
            "source_clock_quality": source_clock_quality or "unknown",
            "receive_clock_quality": receive_clock_quality or "unknown",
            "source_clock_error_ms": source_error,
            "receive_clock_error_ms": receive_error,
            "freshness_verified": pick(row, "freshness_verified", default=False) is True,
            # Do not accept a payload's own eligibility flag.  The parent transport
            # must have issued a sealed attestation bound to the same stream,
            # subscription epoch, provenance identities, and read-only session.
            "execution_eligible": execution_eligible,
        }
        if execution_eligible:
            result.update(
                cohort_now_monotonic_ns=parent_attestation.cohort_now_monotonic_ns,
                cohort_now_epoch=parent_attestation.cohort_now_epoch,
                cohort_now_clock_domain_id=parent_attestation.clock_domain_id,
                cohort_now_receive_clock_error_ms=(
                    parent_attestation.cohort_now_receive_clock_error_ms
                ),
                cohort_now_receive_clock_quality=(parent_attestation.cohort_now_receive_clock_quality),
                cohort_now_freshness_verified=(parent_attestation.cohort_now_freshness_verified),
            )
        return result

    return _ctp_quote_v2_fields
