"""CTP-specific dispatch binding shape; caller retains risk authority and state."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import Any


CTP_MANAGED_RISK_ORDER_SCHEMA = "ctp-managed-risk-order-v1"
CTP_DISPATCH_BINDING_SCHEMA = "ctp-risk-ctp-submit-binding-v1"
CTP_TERMINAL_PROOF_READY_SCHEMA = "ctp-risk-terminal-dispatch-proof-ready-v1"
CTP_TERMINAL_ACK_SCHEMA = "ctp-risk-terminal-dispatch-ack-v1"
CTP_DISPATCH_BINDING_KEYS = frozenset(
    {
        "schema",
        "risk_intent_id",
        "permit_id",
        "risk_claim_digest",
        "risk_intent_hash",
        "submit_command_id",
        "managed_intent_id",
        "runtime_order_id",
        "order_ref",
        "provider_order_id",
        "account_key",
        "scope_key",
        "risk_scope_key",
        "trading_day",
        "reservation_digest_sha256",
        "callback_owner_intent_id",
        "session_generation_id",
        "session_binding_sha256",
        "request_payload_sha256",
        "native_request_payload_sha256",
        "native_request_id",
        "local_queue_receipt_id",
        "instrument_id",
        "exchange_id",
    }
)


def build_ctp_managed_risk_order_payload(intent, mapped, *, is_recovery_close, recovery_proof):
    """Project the CTP execution and Risk intent identities into the journal payload."""
    payload = {
        "schema": CTP_MANAGED_RISK_ORDER_SCHEMA,
        "execution_intent": intent.to_payload(),
        "execution_intent_sha256": intent.fingerprint,
        "risk_intent_sha256": mapped.fingerprint,
    }
    if is_recovery_close:
        assert recovery_proof is not None
        payload["recovery_close"] = {
            "parent_intent_id": recovery_proof.parent_intent_id,
            "parent_permit_id": recovery_proof.parent_permit_id,
            "source_evidence_sha256": recovery_proof.source_evidence_sha256,
            "source_proof_sha256": recovery_proof.fingerprint,
            "source_revision": recovery_proof.source_revision,
        }
    return payload


def validate_ctp_managed_target_order(payload, *, target_intent, target_claim):
    """Match the exact existing CTP target-order shape without issuing authority."""
    return (
        set(payload) == {
            "schema", "execution_intent", "execution_intent_sha256",
            "risk_intent_sha256",
        }
        and payload.get("schema") == CTP_MANAGED_RISK_ORDER_SCHEMA
        and payload.get("execution_intent") == target_intent.to_payload()
        and payload.get("execution_intent_sha256") == target_intent.fingerprint
        and payload.get("risk_intent_sha256") == target_claim.intent_hash
    )


def ctp_native_dispatch_payload_invalid(
    native_payload, *, binding, instrument, exchange_name, payload_sha256,
):
    """Compare a native CTP request with its already-selected dispatch binding."""
    return (
        payload_sha256(native_payload) != binding.native_request_payload_sha256
        or native_payload.get("OrderRef") != binding.order_ref
        or native_payload.get("InstrumentID") != instrument
        or native_payload.get("ExchangeID") != exchange_name
    )


def build_ctp_dispatch_binding_payload(
    *, reservation, session_payload, binding, native_payload, native_request_id,
    risk_intent_id, permit_id, claim, risk_scope_key, callback_owner_intent_id,
    payload_sha256,
):
    """Project the CTP reservation and native dispatch identity for the journal."""
    reservation_payload = {
        "account_key": reservation.account_key,
        "created_at_ns": reservation.created_at_ns,
        "managed_intent_id": reservation.managed_intent_id,
        "order_ref": reservation.order_ref,
        "runtime_order_id": reservation.runtime_order_id,
        "scope_key": reservation.scope_key,
        "trading_day": reservation.trading_day,
    }
    reservation_digest = payload_sha256(
        {
            "registration_type": "ctp_order_identity_registration.v1",
            "reservation": reservation_payload,
            "session_binding": session_payload,
        }
    )
    dispatch_payload = {
        "schema": "ctp-risk-ctp-submit-binding-v1",
        "risk_intent_id": risk_intent_id,
        "permit_id": permit_id,
        "risk_claim_digest": claim.claim_digest,
        "risk_intent_hash": claim.intent_hash,
        "submit_command_id": binding.command_id,
        "managed_intent_id": binding.managed_intent_id,
        "runtime_order_id": binding.runtime_order_id,
        "order_ref": binding.order_ref,
        "provider_order_id": None,
        "account_key": binding.account_key,
        "scope_key": binding.scope_key,
        "risk_scope_key": risk_scope_key,
        "trading_day": binding.trading_day,
        "reservation_digest_sha256": reservation_digest,
        "callback_owner_intent_id": callback_owner_intent_id,
        "session_generation_id": binding.session_generation_id,
        "session_binding_sha256": binding.session_binding_sha256,
        "request_payload_sha256": binding.request_payload_sha256,
        "native_request_payload_sha256": binding.native_request_payload_sha256,
        "native_request_id": native_request_id,
        "local_queue_receipt_id": binding.local_queue_receipt_id,
        "instrument_id": native_payload.get("InstrumentID"),
        "exchange_id": native_payload.get("ExchangeID"),
    }
    return reservation_digest, dispatch_payload


def build_ctp_zero_fill_observations(*, evidence, intent, claim, account_id, risk_intent_id):
    """Build the CTP current-order and zero-fill observation payloads."""
    evidence_payload = evidence.to_payload()
    evidence_sha256 = evidence.evidence_sha256
    # RiskOrderJournal's public observation envelope has a closed
    # top-level allowlist. Keep the CTP-specific current-projection
    # evidence in its bounded `record` member rather than inventing
    # new envelope keys or implying a complete GetDeals history.
    ctp_observation_record = {
        "schema": "ctp-risk-current-order-evidence-v2",
        "evidence_kind": "CTP_CALLBACK_QUERY_ORDER_AND_QUERY_TRADE_TARGET_ORDER_WINDOW",
        "evidence_sha256": evidence_sha256,
        "evidence": evidence_payload,
        "execution_intent_id": intent.intent_id,
        "execution_intent_sha256": intent.fingerprint,
        "risk_intent_id": risk_intent_id,
        "observation_scope": "CURRENT_ORDER_AND_TRADE_QUERY_WINDOW",
        "fill_history_completeness": "COMPLETE_FOR_TARGET_ORDER",
        "trade_history_observed": True,
        "trade_fact_count": evidence.callback_snapshot.trade_fact_count,
        "trade_notional": format(evidence.callback_snapshot.trade_notional, "f"),
        "cumulative_commission": None,
        "cumulative_commission_currency": None,
        "commission_quality": "INCOMPLETE",
    }
    tracked_order = {
        "account_id": account_id,
        "claim_digest": claim.claim_digest,
        "instrument": intent.instrument,
        "position_bucket": intent.position_bucket,
        "position_effect": intent.position_effect.value,
        "position_side": intent.position_side,
        "provider_order_id": evidence.provider_order_id,
        "quantity": format(intent.quantity, "f"),
        "reduce_only": intent.reduce_only,
        "record": {
            **ctp_observation_record,
            "provider_state": "OPEN",
            "provider_status": "ACKED",
        },
        "schema": "ctp-risk-current-order-observation-v1",
        "side": intent.side.value,
        "source_alignment_valid": True,
        "status": "OPEN",
        "filled_quantity": "0",
        "trade_count": 0,
    }
    tracked_fills = {
        "account_id": account_id,
        "claim_digest": claim.claim_digest,
        "filled_quantity": "0",
        "instrument": intent.instrument,
        "provider_order_id": evidence.provider_order_id,
        "record": {
            **ctp_observation_record,
            "requested_quantity": format(intent.quantity, "f"),
        },
        "risk_intent_id": risk_intent_id,
        "schema": "ctp-risk-current-order-zero-fill-observation-v1",
        "source_alignment_valid": True,
        "trade_count": 0,
    }
    return tracked_order, tracked_fills


def ctp_zero_fill_evidence_mismatch(
    evidence, *, intent, dispatch, record, execution_ack_state,
):
    """Compare CTP zero-fill fields; callers retain source and lease authority."""
    return (
        evidence.scope != intent.scope
        or evidence.managed_intent_id != intent.intent_id
        or evidence.intent_fingerprint != intent.fingerprint
        or evidence.provider_order_id != record.provider_order_id
        or evidence.submit_command_id != dispatch.get("submit_command_id")
        or evidence.managed_intent_id != dispatch.get("managed_intent_id")
        or evidence.local_queue_receipt_id != dispatch.get("local_queue_receipt_id")
        or evidence.native_request_id != dispatch.get("native_request_id")
        or evidence.session_binding.session_generation_id
        != dispatch.get("session_generation_id")
        or evidence.session_binding.session_binding_sha256
        != dispatch.get("session_binding_sha256")
        or evidence.projection.account_key != dispatch.get("account_key")
        or evidence.projection.scope_key != dispatch.get("scope_key")
        or evidence.projection.trading_day != dispatch.get("trading_day")
        or evidence.projection.runtime_order_id != dispatch.get("runtime_order_id")
        or evidence.projection.order_ref != dispatch.get("order_ref")
        or evidence.projection.registration_digest
        != dispatch.get("reservation_digest_sha256")
        or evidence.projection.instrument_id != dispatch.get("instrument_id")
        or evidence.projection.exchange_id != dispatch.get("exchange_id")
        or evidence.quantity != intent.quantity
        or evidence.projection.quantity != evidence.quantity
        or evidence.callback_snapshot.owner_intent_id
        != dispatch.get("callback_owner_intent_id")
        or evidence.callback_snapshot.session_generation_id
        != dispatch.get("session_generation_id")
        or evidence.provider_observation.state is not execution_ack_state
        or evidence.provider_observation.filled_quantity != 0
        or evidence.projection.provider_state != "OPEN"
        or evidence.projection.traded_quantity != 0
        or evidence.projection.remaining_quantity != evidence.quantity
        or evidence.callback_snapshot.trade_notional != 0
        or evidence.callback_snapshot.trade_fact_count != 0
        or evidence.callback_snapshot.provider_state != "ACKNOWLEDGED"
        or evidence.callback_snapshot.filled_quantity != 0
    )


def validate_ctp_dispatch_binding_claim(
    value: object,
    *,
    claim: Any,
    risk_scope_key: str,
    require_sha256,
) -> bool:
    """Validate the CTP dispatch extension without widening generic history."""

    if type(value) is not dict or set(value) != CTP_DISPATCH_BINDING_KEYS:
        return False
    digest_fields = {
        "risk_claim_digest",
        "risk_intent_hash",
        "reservation_digest_sha256",
        "session_binding_sha256",
        "request_payload_sha256",
        "native_request_payload_sha256",
    }
    try:
        for key in digest_fields:
            require_sha256(value.get(key), "CTP dispatch binding " + key)
    except ValueError:
        return False
    string_fields = CTP_DISPATCH_BINDING_KEYS - {
        "schema",
        "provider_order_id",
        "native_request_id",
        *digest_fields,
    }
    return not (
        value.get("schema") != CTP_DISPATCH_BINDING_SCHEMA
        or value.get("risk_intent_id") != claim.intent_id
        or value.get("permit_id") != claim.permit_id
        or value.get("risk_claim_digest") != claim.claim_digest
        or value.get("risk_intent_hash") != claim.intent_hash
        or value.get("risk_scope_key") != risk_scope_key
        or value.get("provider_order_id") is not None
        or any(type(value.get(key)) is not str or not value.get(key) for key in string_fields)
        or type(value.get("native_request_id")) is not int
        or value.get("native_request_id") <= 0
    )


def _ctp_payload_sha256(value: Any) -> str | None:
    """Hash the exact JSON contract encoding used by Execution payload_sha256."""

    try:
        encoded = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
    except (TypeError, ValueError):
        return None
    return hashlib.sha256(encoded.encode("utf-8", errors="strict")).hexdigest()


def validate_ctp_zero_fill_dispatch_history(
    value: object,
    *,
    claim: Any,
    risk_scope_key: str,
    command: Any,
    proof: Any,
    reference: Mapping[str, Any],
    order_event: Any,
    fill_event: Any,
    journal_command_type,
    risk_tracked_proof_type,
    journal_event_type,
    canonical_json,
    validate_claim_binding,
) -> bool:
    """Bind a CTP DISPATCHING identity to its exact zero-fill evidence pair."""

    if (
        not validate_claim_binding(
            value, claim=claim, risk_scope_key=risk_scope_key
        )
        or type(command) is not journal_command_type
        or type(proof) is not risk_tracked_proof_type
        or type(order_event) is not journal_event_type
        or type(fill_event) is not journal_event_type
        or order_event.event_type != "ORDER_OBSERVATION"
        or fill_event.event_type != "FILL_OBSERVATION"
        or type(order_event.payload) is not dict
        or type(fill_event.payload) is not dict
    ):
        return False

    binding = value
    order = order_event.payload
    fills = fill_event.payload
    order_record = order.get("record")
    fill_record = fills.get("record")
    common_record_fields = {
        "schema",
        "evidence_kind",
        "evidence_sha256",
        "evidence",
        "execution_intent_id",
        "execution_intent_sha256",
        "risk_intent_id",
        "observation_scope",
        "fill_history_completeness",
        "trade_history_observed",
        "trade_fact_count",
        "trade_notional",
        "cumulative_commission",
        "cumulative_commission_currency",
        "commission_quality",
    }
    if (
        type(order_record) is not dict
        or type(fill_record) is not dict
        or set(order_record) != common_record_fields | {"provider_state", "provider_status"}
        or set(fill_record) != common_record_fields | {"requested_quantity"}
    ):
        return False
    order_common = {key: item for key, item in order_record.items() if key in common_record_fields}
    fill_common = {key: item for key, item in fill_record.items() if key in common_record_fields}
    if canonical_json(order_common) != canonical_json(fill_common):
        return False

    evidence = order_record.get("evidence")
    evidence_schema = evidence.get("schema") if type(evidence) is dict else None
    has_trade_window = evidence_schema == "ctp_zero_fill_tracked_order_evidence.v2"
    evidence_fields = {
        "schema",
        "scope",
        "intent",
        "reservation",
        "submit",
        "session_binding",
        "callback_snapshot",
        "provider_observation",
        "query_projection",
        "verified_at_ns",
        "expires_at_ns",
        "evidence_sha256",
    }
    if has_trade_window:
        evidence_fields.add("query_trade")
    if (
        type(evidence) is not dict
        or set(evidence) != evidence_fields
        or evidence_schema
        not in {
            "ctp_zero_fill_tracked_order_evidence.v1",
            "ctp_zero_fill_tracked_order_evidence.v2",
        }
        or type(evidence.get("evidence_sha256")) is not str
        or _ctp_payload_sha256(
            {key: item for key, item in evidence.items() if key != "evidence_sha256"}
        )
        != evidence.get("evidence_sha256")
        or order_record.get("evidence_sha256") != evidence.get("evidence_sha256")
        or order_record.get("schema")
        != (
            "ctp-risk-current-order-evidence-v2"
            if has_trade_window
            else "ctp-risk-current-order-evidence-v1"
        )
        or order_record.get("evidence_kind")
        != (
            "CTP_CALLBACK_QUERY_ORDER_AND_QUERY_TRADE_TARGET_ORDER_WINDOW"
            if has_trade_window
            else "CTP_CALLBACK_AND_QUERY_ORDER_CURRENT_STATE"
        )
        or order_record.get("observation_scope")
        != (
            "CURRENT_ORDER_AND_TRADE_QUERY_WINDOW"
            if has_trade_window
            else "CURRENT_ORDER_PROJECTION"
        )
        or order_record.get("fill_history_completeness")
        != ("COMPLETE_FOR_TARGET_ORDER" if has_trade_window else "UNOBSERVED")
        or order_record.get("trade_history_observed") is not has_trade_window
        or order_record.get("trade_fact_count") != 0
        or order_record.get("trade_notional") != "0"
        or order_record.get("cumulative_commission") is not None
        or order_record.get("cumulative_commission_currency") is not None
        or order_record.get("commission_quality") != "INCOMPLETE"
    ):
        return False

    scope = evidence.get("scope")
    intent = evidence.get("intent")
    reservation = evidence.get("reservation")
    submit = evidence.get("submit")
    session = evidence.get("session_binding")
    callback = evidence.get("callback_snapshot")
    observation = evidence.get("provider_observation")
    query = evidence.get("query_projection")
    query_trade = evidence.get("query_trade") if has_trade_window else None
    if any(
        type(item) is not dict
        for item in (
            scope,
            intent,
            reservation,
            submit,
            session,
            callback,
            observation,
            query,
        )
    ) or (has_trade_window and type(query_trade) is not dict):
        return False

    if (
        set(scope) != {"account_ref", "environment", "provider", "strategy_id", "trading_day"}
        or set(intent)
        != {
            "intent_id",
            "fingerprint",
            "instrument_id",
            "position_effect",
            "position_side",
            "position_bucket",
            "reduce_only",
            "quantity",
        }
        or set(reservation)
        != {
            "account_key",
            "trading_day",
            "scope_key",
            "managed_intent_id",
            "runtime_order_id",
            "order_ref",
            "created_at_ns",
        }
        or set(submit)
        != {
            "command_id",
            "status",
            "local_dispatch_outcome",
            "local_queue_receipt_id",
            "native_request_id",
            "dispatch_binding_sha256",
            "session_binding_sha256",
        }
        or set(query) != {"projection_id", "projection_sha256", "projection"}
    ):
        return False

    scope_key = "scope:" + str(_ctp_payload_sha256(scope))
    account_key = "account:" + str(
        _ctp_payload_sha256(
            {
                "provider": scope.get("provider"),
                "environment": scope.get("environment"),
                "account_ref": scope.get("account_ref"),
            }
        )
    )
    # Recreate the Store's callback binding payload, whose source tags are
    # nested and whose source generation differs from the live connection one.
    session_payload = {
        "binding_type": "ctp_callback_session_binding.v1",
        **{key: session.get(key) for key in (
            "owner_intent_id", "account_key", "scope_key", "trading_day",
            "session_generation_id", "dispatch_front_id", "dispatch_session_id",
            "source_high_watermark",
        )},
        "source_tags": {
            **{key: session.get(key) for key in (
                "source_instance_id", "native_client_epoch", "native_api_source_id",
                "native_spi_source_id", "native_api_generation",
            )},
            "connection_generation": session.get("source_connection_generation"),
        },
    }
    if _ctp_payload_sha256(session_payload) != session.get("session_binding_sha256"):
        return False
    canonical = command.canonical_payload
    canonical_intent = canonical.get("execution_intent")
    if (
        canonical.get("schema") != CTP_MANAGED_RISK_ORDER_SCHEMA
        or type(canonical_intent) is not dict
        or canonical.get("execution_intent_sha256") != intent.get("fingerprint")
        or canonical_intent.get("scope") != scope
        or canonical_intent.get("intent_id") != intent.get("intent_id")
        or canonical_intent.get("instrument") != intent.get("instrument_id")
        or canonical_intent.get("position_effect") != "OPEN"
        or canonical_intent.get("reduce_only") is not False
        or intent.get("position_effect") != "OPEN"
        or intent.get("position_side") != "NET"
        or intent.get("position_bucket") != "ALL"
        or intent.get("reduce_only") is not False
    ):
        return False
    expected_reservation_digest = _ctp_payload_sha256(
        {
            "registration_type": "ctp_order_identity_registration.v1",
            "reservation": reservation,
            "session_binding": session_payload,
        }
    )
    projection = query.get("projection")
    if type(projection) is not dict:
        return False
    provenance = projection.get("query_provenance")
    try:
        intent_quantity = Decimal(str(intent.get("quantity")))
        callback_notional = Decimal(str(callback.get("trade_notional")))
    except (InvalidOperation, TypeError, ValueError):
        return False
    if not intent_quantity.is_finite() or intent_quantity <= 0 or not callback_notional.is_finite():
        return False

    if has_trade_window:
        trade_window_fields = {
            "schema",
            "instrument_id",
            "account_key",
            "scope_key",
            "trading_day",
            "managed_intent_id",
            "runtime_order_id",
            "order_ref",
            "target_order_sys_id",
            "owner_intent_id",
            "session_binding_sha256",
            "session_generation_id",
            "connection_generation",
            "query_request_id",
            "record_count",
            "records_sha256",
            "filters_sha256",
            "complete",
            "is_last_seen",
            "timed_out",
            "unsupported",
            "error_code",
            "late_callback_count",
            "store_fence_id",
            "store_window_sha256",
            "store_start_source_sequence",
            "store_end_source_sequence",
            "store_applied_source_sequence",
            "store_observed_at_ns",
            "query_started_monotonic",
            "query_completed_monotonic",
            "target_order_trade_count",
        }
        query_started = query_trade.get("query_started_monotonic")
        query_completed = query_trade.get("query_completed_monotonic")
        sequence_start = query_trade.get("store_start_source_sequence")
        sequence_end = query_trade.get("store_end_source_sequence")
        record_count = query_trade.get("record_count")
        query_request_id = query_trade.get("query_request_id")
        connection_generation = query_trade.get("connection_generation")
        hashes = (
            query_trade.get("records_sha256"),
            query_trade.get("filters_sha256"),
            query_trade.get("session_binding_sha256"),
            query_trade.get("store_window_sha256"),
        )
        if (
            set(query_trade) != trade_window_fields
            or query_trade.get("schema") != "ctp_order_target_trade_query_window.v1"
            or type(query_trade.get("instrument_id")) is not str
            or not query_trade.get("instrument_id")
            or type(query_trade.get("target_order_sys_id")) is not str
            or not query_trade.get("target_order_sys_id")
            or type(query_trade.get("store_fence_id")) is not str
            or not query_trade.get("store_fence_id")
            or any(
                type(query_trade.get(name)) is not str
                or not query_trade.get(name)
                for name in (
                    "account_key",
                    "scope_key",
                    "trading_day",
                    "managed_intent_id",
                    "runtime_order_id",
                    "order_ref",
                    "owner_intent_id",
                    "session_generation_id",
                )
            )
            or any(
                type(value) is not str
                or len(value) != 64
                or any(character not in "0123456789abcdef" for character in value)
                for value in hashes
            )
            or type(query_request_id) is not int
            or not 0 < query_request_id <= 2_147_483_647
            or type(record_count) is not int
            or not 0 <= record_count <= 128
            or type(connection_generation) is not int
            or connection_generation <= 0
            or query_trade.get("complete") is not True
            or query_trade.get("is_last_seen") is not True
            or query_trade.get("timed_out") is not False
            or query_trade.get("unsupported") is not False
            or query_trade.get("error_code") is not None
            and type(query_trade.get("error_code")) is not int
            or query_trade.get("error_code") not in (None, 0)
            or type(query_trade.get("late_callback_count")) is not int
            or query_trade.get("late_callback_count") != 0
            or type(sequence_start) is not int
            or sequence_start < 0
            or type(sequence_end) is not int
            or sequence_end <= sequence_start
            or sequence_end - sequence_start not in {record_count, record_count + 1}
            or query_trade.get("store_applied_source_sequence") != sequence_end
            or type(query_trade.get("store_observed_at_ns")) is not int
            or query_trade.get("store_observed_at_ns") <= 0
            or type(query_started) not in (int, float)
            or type(query_started) is bool
            or not math.isfinite(query_started)
            or type(query_completed) not in (int, float)
            or type(query_completed) is bool
            or not math.isfinite(query_completed)
            or query_started <= 0
            or query_completed < query_started
            or query_trade.get("store_observed_at_ns") / 1_000_000_000
            < query_completed
            or query_trade.get("target_order_trade_count") != 0
            or type(query_trade.get("target_order_trade_count")) is not int
            or query_trade.get("account_key") != reservation.get("account_key")
            or query_trade.get("scope_key") != scope_key
            or query_trade.get("trading_day") != scope.get("trading_day")
            or query_trade.get("managed_intent_id") != intent.get("intent_id")
            or query_trade.get("runtime_order_id") != reservation.get("runtime_order_id")
            or query_trade.get("order_ref") != reservation.get("order_ref")
            or query_trade.get("instrument_id") != intent.get("instrument_id")
            or query_trade.get("target_order_sys_id") != callback.get("order_sys_id")
            or query_trade.get("owner_intent_id") != session.get("owner_intent_id")
            or query_trade.get("session_binding_sha256")
            != session.get("session_binding_sha256")
            or query_trade.get("session_generation_id")
            != session.get("session_generation_id")
            or query_trade.get("connection_generation")
            != session.get("connection_generation")
        ):
            return False

    return not (
        binding.get("reservation_digest_sha256") != expected_reservation_digest
        or binding.get("account_key") != account_key
        or binding.get("account_key") != reservation.get("account_key")
        or binding.get("scope_key") != scope_key
        or binding.get("scope_key") != reservation.get("scope_key")
        or binding.get("trading_day") != scope.get("trading_day")
        or binding.get("trading_day") != reservation.get("trading_day")
        or binding.get("managed_intent_id") != intent.get("intent_id")
        or binding.get("managed_intent_id") != reservation.get("managed_intent_id")
        or binding.get("runtime_order_id") != reservation.get("runtime_order_id")
        or binding.get("order_ref") != reservation.get("order_ref")
        or binding.get("submit_command_id") != submit.get("command_id")
        or binding.get("local_queue_receipt_id") != submit.get("local_queue_receipt_id")
        or binding.get("native_request_id") != submit.get("native_request_id")
        or binding.get("session_generation_id") != session.get("session_generation_id")
        or binding.get("session_binding_sha256") != session.get("session_binding_sha256")
        or binding.get("session_binding_sha256") != submit.get("session_binding_sha256")
        or binding.get("callback_owner_intent_id") != session.get("owner_intent_id")
        or binding.get("instrument_id") != intent.get("instrument_id")
        or binding.get("exchange_id") != callback.get("exchange_id")
        # The CTP digest belongs to the managed execution request domain.  It
        # is not journal_command_type.order_request_sha256, which hashes Risk's
        # normalized provider request.  For submit bindings the native payload
        # is the exact logical payload, so the two CTP digests must agree.
        or binding.get("request_payload_sha256")
        != binding.get("native_request_payload_sha256")
        or intent.get("intent_id") != command.intent_id
        or intent.get("intent_id") != claim.intent_id
        or intent.get("fingerprint") != order_record.get("execution_intent_sha256")
        or order_record.get("execution_intent_id") != command.intent_id
        or order_record.get("risk_intent_id") != command.risk_intent_id
        or order_record.get("risk_intent_id") != claim.intent_id
        or order_record.get("execution_intent_id") != fill_record.get("execution_intent_id")
        or order_record.get("execution_intent_sha256") != fill_record.get("execution_intent_sha256")
        or order_record.get("risk_intent_id") != fill_record.get("risk_intent_id")
        or order.get("schema") != "ctp-risk-current-order-observation-v1"
        or fills.get("schema") != "ctp-risk-current-order-zero-fill-observation-v1"
        or order.get("account_id") != command.provider_account_id
        or fills.get("account_id") != command.provider_account_id
        or order.get("claim_digest") != claim.claim_digest
        or fills.get("claim_digest") != claim.claim_digest
        or order.get("risk_intent_id") not in (None, command.risk_intent_id)
        or fills.get("risk_intent_id") != command.risk_intent_id
        or order.get("instrument") != intent.get("instrument_id")
        or fills.get("instrument") != intent.get("instrument_id")
        or order.get("provider_order_id") != observation.get("provider_order_id")
        or fills.get("provider_order_id") != observation.get("provider_order_id")
        or proof.intent_id != claim.intent_id
        or proof.provider_order_id != observation.get("provider_order_id")
        or type(evidence.get("verified_at_ns")) is not int
        or evidence.get("verified_at_ns") <= 0
        or type(evidence.get("expires_at_ns")) is not int
        or evidence.get("expires_at_ns") <= evidence.get("verified_at_ns")
        or session.get("account_key") != account_key
        or session.get("scope_key") != scope_key
        or session.get("trading_day") != scope.get("trading_day")
        or callback.get("account_key") != account_key
        or callback.get("scope_key") != scope_key
        or callback.get("trading_day") != scope.get("trading_day")
        or callback.get("command_id") != submit.get("command_id")
        or callback.get("command_status") != "COMPLETED"
        or callback.get("local_dispatch_outcome") != "QUEUED"
        or callback.get("owner_intent_id") != session.get("owner_intent_id")
        or callback.get("session_generation_id") != session.get("session_generation_id")
        or callback.get("order_ref") != reservation.get("order_ref")
        or callback.get("request_id") != binding.get("native_request_id")
        or callback.get("provider_state") != "ACKNOWLEDGED"
        or callback.get("volume_traded") != 0
        or callback.get("filled_quantity") != 0
        or callback.get("trade_fact_count") != 0
        or callback_notional != 0
        or observation.get("intent_id") != command.intent_id
        or observation.get("state") != "ACKED"
        or observation.get("filled_quantity") != "0"
        or observation.get("average_price") is not None
        or projection.get("account_key") != account_key
        or type(provenance) is not dict
        or provenance.get("scope_key") != scope_key
        or projection.get("managed_intent_id") != intent.get("intent_id")
        or projection.get("runtime_order_id") != reservation.get("runtime_order_id")
        or projection.get("order_ref") != reservation.get("order_ref")
        or projection.get("exchange_id") != binding.get("exchange_id")
        or projection.get("instrument_id") != intent.get("instrument_id")
        or projection.get("provider_state") != "OPEN"
        or projection.get("traded_quantity") != 0
        or projection.get("remaining_quantity") != intent.get("quantity")
        or projection.get("query_complete") is not True
        or query.get("projection_sha256") != _ctp_payload_sha256(projection)
        or order_record.get("provider_state") != "OPEN"
        or order_record.get("provider_status") != "ACKED"
        or order.get("status") != "OPEN"
        or order.get("filled_quantity") != "0"
        or fills.get("filled_quantity") != "0"
        or order.get("quantity") != fills.get("record", {}).get("requested_quantity")
        or order.get("quantity") != format(intent_quantity, "f")
        or canonical_intent.get("quantity") != format(intent_quantity, "f")
        or fills.get("record", {}).get("requested_quantity") != format(intent_quantity, "f")
        or fills.get("trade_count") != 0
        or order.get("trade_count") != 0
        or order.get("source_alignment_valid") is not True
        or fills.get("source_alignment_valid") is not True
        or reference.get("order_observation_revision") != order_event.revision
        or reference.get("fill_observation_revision") != fill_event.revision
        or reference.get("order_observation_record_sha256") != order_event.record_sha256
        or reference.get("fill_observation_record_sha256") != fill_event.record_sha256
    )


CTP_TERMINAL_EVIDENCE_SCHEMA = "ctp_terminal_order_query_evidence.v1"
CTP_TERMINAL_EVIDENCE_FIELDS = frozenset(
    {
        "terminal_state",
        "account_key",
        "scope_key",
        "trading_day",
        "managed_intent_id",
        "runtime_order_id",
        "order_ref",
        "submit_command_id",
        "provider_order_id",
        "instrument_id",
        "exchange_id",
        "order_sys_id",
        "callback_owner_intent_id",
        "session_generation_id",
        "callback_stream_id",
        "session_binding_sha256",
        "order_source_sequence",
        "terminal_ingress_record_digest_sha256",
        "terminal_source_digest_sha256",
        "request_id",
        "front_id",
        "session_id",
        "quantity",
        "volume_traded",
        "filled_quantity",
        "trade_fact_count",
        "trade_notional",
        "commission_quality",
        "cancel_command_id",
        "cancel_action_state",
        "reservation_digest_sha256",
        "query_fence_id",
        "query_start_source_sequence",
        "query_end_source_sequence",
        "query_applied_source_sequence",
        "query_request_id",
        "query_filter_sha256",
        "query_records_sha256",
        "query_source_sha256",
        "query_window_sha256",
        "query_callback_ingress_digests",
        "query_account_fingerprint_sha256",
        "query_connection_generation",
        "query_started_at_utc",
        "query_completed_at_utc",
        "query_started_monotonic",
        "query_completed_monotonic",
        "verified_at_ns",
        "expires_at_ns",
        "evidence_sha256",
    }
)


def validate_ctp_terminal_evidence(
    evidence: object,
    binding: Mapping[str, Any],
    command: Any,
    fence: Any,
    *,
    journal_error_type,
    hash_text,
    canonical_json,
    require_sha256,
    sha256_length,
) -> str:
    if (
        type(evidence) is not dict
        or set(evidence) != ({"schema"} | CTP_TERMINAL_EVIDENCE_FIELDS)
        or evidence.get("schema") != CTP_TERMINAL_EVIDENCE_SCHEMA
    ):
        raise journal_error_type("CTP_TERMINAL_EVIDENCE_SCHEMA_INVALID")
    body = {key: value for key, value in evidence.items() if key != "evidence_sha256"}
    evidence_sha256 = hash_text(canonical_json(body))
    try:
        require_sha256(evidence.get("evidence_sha256"), "CTP terminal evidence digest")
        if evidence_sha256 != evidence["evidence_sha256"]:
            raise journal_error_type("CTP_TERMINAL_EVIDENCE_DIGEST_MISMATCH")
        for key in (
            "session_binding_sha256",
            "terminal_ingress_record_digest_sha256",
            "terminal_source_digest_sha256",
            "reservation_digest_sha256",
            "query_filter_sha256",
            "query_records_sha256",
            "query_source_sha256",
            "query_window_sha256",
            "query_account_fingerprint_sha256",
        ):
            require_sha256(evidence[key], "CTP terminal evidence " + key)
        callback_digests = evidence["query_callback_ingress_digests"]
        if (
            type(callback_digests) is not list
            or not callback_digests
            or len(callback_digests) > 256
            or any(
                type(value) is not str
                or len(value) != sha256_length
                or any(char not in "0123456789abcdef" for char in value)
                for value in callback_digests
            )
        ):
            raise journal_error_type("CTP_TERMINAL_QUERY_CALLBACKS_INVALID")
        quantity = Decimal(evidence["quantity"])
        zero_values = tuple(
            Decimal(evidence[name])
            for name in ("volume_traded", "filled_quantity", "trade_notional")
        )
    except (KeyError, TypeError, ValueError, InvalidOperation):
        raise journal_error_type("CTP_TERMINAL_EVIDENCE_FIELDS_INVALID") from None
    if (
        not quantity.is_finite()
        or quantity <= 0
        or any(not value.is_finite() or value != 0 for value in zero_values)
        or type(evidence.get("trade_fact_count")) is not int
        or evidence["trade_fact_count"] != 0
        or evidence.get("commission_quality") != "INCOMPLETE"
        or evidence.get("terminal_state") not in {"CANCELLED", "REJECTED"}
    ):
        raise journal_error_type("CTP_TERMINAL_NOT_ZERO_FILL")

    matching_fields = (
        "account_key",
        "scope_key",
        "trading_day",
        "managed_intent_id",
        "runtime_order_id",
        "order_ref",
        "submit_command_id",
        "instrument_id",
        "exchange_id",
        "callback_owner_intent_id",
        "session_generation_id",
        "session_binding_sha256",
        "reservation_digest_sha256",
    )
    if any(
        type(evidence.get(key)) is not str
        or not evidence.get(key)
        or evidence.get(key) != binding.get(key)
        for key in matching_fields
    ):
        raise journal_error_type("CTP_TERMINAL_DISPATCH_BINDING_MISMATCH")
    if (
        binding.get("risk_scope_key") != fence.scope.key
        or evidence.get("provider_order_id") is None
        or type(evidence.get("provider_order_id")) is not str
        or not evidence.get("provider_order_id")
        or type(evidence.get("order_sys_id")) is not str
        or not evidence.get("order_sys_id")
        or evidence.get("cancel_action_state")
        not in {None, "ACKNOWLEDGED", "REJECTED", "TERMINAL"}
        or (evidence.get("cancel_command_id") is None)
        != (evidence.get("cancel_action_state") is None)
        or (
            evidence.get("terminal_state") == "CANCELLED"
            and evidence.get("cancel_action_state") not in {None, "ACKNOWLEDGED", "TERMINAL"}
        )
        or (
            evidence.get("terminal_state") == "REJECTED"
            and evidence.get("cancel_action_state") not in {None, "REJECTED"}
        )
    ):
        raise journal_error_type("CTP_TERMINAL_NATIVE_IDENTITY_INVALID")
    instrument = command.order_request.get("symbol", command.order_request.get("instrument"))
    if (
        instrument != evidence.get("instrument_id")
        or command.risk_intent_id != binding.get("risk_intent_id")
        or command.intent_id != binding.get("managed_intent_id")
    ):
        raise journal_error_type("CTP_TERMINAL_COMMAND_MISMATCH")

    if (
        type(evidence.get("query_fence_id")) is not str
        or not evidence.get("query_fence_id")
        or type(evidence.get("query_request_id")) is not int
        or evidence["query_request_id"] <= 0
        or type(evidence.get("request_id")) is not int
        or evidence["request_id"] <= 0
        or type(evidence.get("front_id")) is not int
        or evidence["front_id"] < 0
        or type(evidence.get("session_id")) is not int
        or evidence["session_id"] < 0
        or any(
            type(evidence.get(name)) is not int or evidence[name] <= 0
            for name in (
                "query_start_source_sequence",
                "query_end_source_sequence",
                "query_applied_source_sequence",
                "query_connection_generation",
                "order_source_sequence",
                "verified_at_ns",
                "expires_at_ns",
            )
        )
        or evidence["query_end_source_sequence"] <= evidence["query_start_source_sequence"]
        or evidence["query_end_source_sequence"] != evidence["query_applied_source_sequence"]
        or evidence["query_end_source_sequence"] - evidence["query_start_source_sequence"]
        != len(callback_digests)
        or evidence["expires_at_ns"] <= evidence["verified_at_ns"]
        or type(evidence.get("query_started_monotonic")) not in (int, float)
        or type(evidence.get("query_completed_monotonic")) not in (int, float)
        or evidence["query_completed_monotonic"] < evidence["query_started_monotonic"]
        or type(evidence.get("query_started_at_utc")) is not str
        or type(evidence.get("query_completed_at_utc")) is not str
    ):
        raise journal_error_type("CTP_TERMINAL_QUERY_WINDOW_INVALID")
    return evidence_sha256


def validate_ctp_terminal_dispatch_binding(
    binding, command, claim, fence, *, require_sha256, journal_error_type
) -> None:
    if type(binding) is not dict or set(binding) != CTP_DISPATCH_BINDING_KEYS:
        raise journal_error_type("TERMINAL_DISPATCH_CTP_BINDING_INVALID")
    for key in (
        "risk_claim_digest",
        "risk_intent_hash",
        "reservation_digest_sha256",
        "session_binding_sha256",
        "request_payload_sha256",
        "native_request_payload_sha256",
    ):
        require_sha256(binding.get(key), "CTP dispatch binding " + key)
    nonempty_string_fields = CTP_DISPATCH_BINDING_KEYS - {
        "schema",
        "provider_order_id",
        "risk_claim_digest",
        "risk_intent_hash",
        "reservation_digest_sha256",
        "session_binding_sha256",
        "request_payload_sha256",
        "native_request_payload_sha256",
        "native_request_id",
    }
    if (
        binding.get("schema") != CTP_DISPATCH_BINDING_SCHEMA
        or binding.get("risk_intent_id") != command.risk_intent_id
        or binding.get("permit_id") != claim.permit_id
        or binding.get("risk_claim_digest") != claim.claim_digest
        or binding.get("risk_intent_hash") != claim.intent_hash
        or binding.get("managed_intent_id") != command.intent_id
        or binding.get("provider_order_id") is not None
        or binding.get("risk_scope_key") != fence.scope.key
        or any(
            type(binding.get(key)) is not str or not binding.get(key)
            for key in nonempty_string_fields
        )
        or type(binding.get("native_request_id")) is not int
        or binding.get("native_request_id") <= 0
    ):
        raise journal_error_type("TERMINAL_DISPATCH_CTP_BINDING_INVALID")


def validate_ctp_terminal_observations(
    order_event, fill_event, evidence_sha256, command, claim, *, claim_payload, journal_error_type
) -> None:
    if (
        set(order_event.payload)
        != {
            "schema",
            "claim",
            "permit_id",
            "risk_intent_id",
            "dispatch_attempt_count",
            "evidence_sha256",
            "ctp_terminal_evidence",
            "updated_at_ns",
        }
        or type(order_event.payload.get("updated_at_ns")) is not int
        or order_event.payload.get("updated_at_ns") <= 0
        or
        order_event.payload.get("schema") != "ctp-risk-terminal-dispatch-order-observation-v1"
        or order_event.payload.get("claim") != claim_payload(claim)
        or order_event.payload.get("permit_id") != claim.permit_id
        or order_event.payload.get("risk_intent_id") != command.risk_intent_id
        or order_event.payload.get("dispatch_attempt_count") != 1
        or type(order_event.payload.get("dispatch_attempt_count")) is not int
        or order_event.payload.get("evidence_sha256") != evidence_sha256
    ):
        raise journal_error_type("TERMINAL_DISPATCH_ORDER_OBSERVATION_INVALID")
    expected_fill = {
        "schema": "ctp-risk-terminal-dispatch-fill-observation-v1",
        "claim": claim_payload(claim),
        "dispatch_attempt_count": 1,
        "permit_id": claim.permit_id,
        "risk_intent_id": command.risk_intent_id,
        "evidence_sha256": evidence_sha256,
        "commission_quality": "INCOMPLETE",
        "fee_history_complete": True,
        "fee_record_count": 0,
        "filled_quantity": 0,
        "records": [],
        "trade_count": 0,
    }
    fill_payload = dict(fill_event.payload)
    fill_updated_at_ns = fill_payload.pop("updated_at_ns", None)
    if (
        set(fill_event.payload) != set(expected_fill) | {"updated_at_ns"}
        or fill_payload != expected_fill
        or type(fill_updated_at_ns) is not int
        or fill_updated_at_ns <= 0
    ):
        raise journal_error_type("TERMINAL_DISPATCH_FILL_OBSERVATION_INVALID")


def _terminal_state(evidence):
    return "CANCELED_NO_FILL" if evidence["terminal_state"] == "CANCELLED" else "REJECTED_NO_FILL"


def ctp_risk_dispatch_evidence_profile():
    """Declare the existing CTP journal evidence shape without creating authority."""
    from bt_api_base.risk_dispatch_evidence import RiskDispatchEvidenceProfile

    return RiskDispatchEvidenceProfile(
        provider="ctp",
        binding_field="ctp_dispatch_binding",
        terminal_evidence_field="ctp_terminal_evidence",
        terminal_proof_schema=CTP_TERMINAL_PROOF_READY_SCHEMA,
        terminal_ack_schema=CTP_TERMINAL_ACK_SCHEMA,
        validate_claim=validate_ctp_dispatch_binding_claim,
        validate_zero_fill=validate_ctp_zero_fill_dispatch_history,
        validate_terminal_binding=validate_ctp_terminal_dispatch_binding,
        validate_terminal_evidence=validate_ctp_terminal_evidence,
        validate_terminal_observations=validate_ctp_terminal_observations,
        terminal_state=_terminal_state,
        target_order_schema=CTP_MANAGED_RISK_ORDER_SCHEMA,
        validate_target_order=validate_ctp_managed_target_order,
    )


__all__ = (
    "CTP_MANAGED_RISK_ORDER_SCHEMA",
    "build_ctp_managed_risk_order_payload",
    "validate_ctp_managed_target_order",
    "ctp_native_dispatch_payload_invalid",
    "build_ctp_dispatch_binding_payload",
    "build_ctp_zero_fill_observations",
    "ctp_zero_fill_evidence_mismatch",
    "CTP_DISPATCH_BINDING_SCHEMA",
    "CTP_DISPATCH_BINDING_KEYS",
    "CTP_TERMINAL_EVIDENCE_SCHEMA",
    "CTP_TERMINAL_EVIDENCE_FIELDS",
    "CTP_TERMINAL_PROOF_READY_SCHEMA",
    "CTP_TERMINAL_ACK_SCHEMA",
    "validate_ctp_dispatch_binding_claim",
    "validate_ctp_zero_fill_dispatch_history",
    "validate_ctp_terminal_evidence",
    "validate_ctp_terminal_dispatch_binding",
    "validate_ctp_terminal_observations",
    "ctp_risk_dispatch_evidence_profile",
)
