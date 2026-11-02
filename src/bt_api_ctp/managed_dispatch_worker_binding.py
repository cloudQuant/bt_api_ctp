"""CTP-native request and identity binding for the managed Execution worker."""
from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from bt_api_execution.contracts import ExecutionScope
from bt_api_execution.errors import ContractValidationError
from bt_api_execution.store import (
    CtpCallbackSessionOwnerHandle,
    CtpCancelTarget,
    CtpDispatchCommand,
    CtpDispatchReceipt,
    CtpOrderIdentityReservation,
    CtpOrderTargetProjectionHandle,
    CtpVerifiedOrderTargetProjection,
)
from .managed_dispatch_contracts import (
    CtpManagedDispatchBinding,
    CtpManagedPreparedDispatch,
    _binding_matches_command,
)

_HEX_32 = re.compile(r"^[0-9a-f]{32}$", re.ASCII)


class CtpManagedDispatchBindingMixin:
    """Apply provider-native request bindings to the worker's durable store."""

    def stage_prepared_dispatch(
        self,
        prepared: CtpManagedPreparedDispatch,
        *,
        cancel_target_projection: CtpOrderTargetProjectionHandle | None = None,
        position_baseline_authorization: object | None = None,
        recovery_close_authorization: object | None = None,
    ) -> CtpManagedDispatchBinding:
        if type(prepared) is not CtpManagedPreparedDispatch:
            raise ContractValidationError("typed managed CTP prepared dispatch is required")
        if (
            position_baseline_authorization is not None
            and recovery_close_authorization is not None
        ):
            raise ContractValidationError("CTP position dispatch authorizations are exclusive")
        if prepared.operation != "submit" and (
            position_baseline_authorization is not None
            or recovery_close_authorization is not None
        ):
            raise ContractValidationError("position dispatch authorization is SUBMIT-only")
        reservation = self._store.read_ctp_order_identity(self._scope, prepared.managed_intent_id)
        if (
            type(reservation) is not CtpOrderIdentityReservation
            or reservation != prepared.order_ref_reservation
            or reservation.runtime_order_id != prepared.runtime_order_id
            or reservation.order_ref != prepared.order_ref
        ):
            raise ContractValidationError(
                "prepared OrderRef does not match a committed same-store reservation"
            )
        if prepared.request_payload.get("OrderRef") != reservation.order_ref:
            raise ContractValidationError("prepared request OrderRef differs from reservation")
        cancel_target = None
        if prepared.operation == "cancel":
            cancel_target = CtpCancelTarget(
                order_ref=reservation.order_ref,
                exchange_id=prepared.cancel_target_exchange_id or "",
                order_sys_id=prepared.cancel_target_order_sys_id or "",
                front_id=prepared.cancel_target_front_id or 0,
                session_id=prepared.cancel_target_session_id or 0,
            )
        command = self._store.stage_ctp_dispatch_command(
            self._scope,
            prepared.command_id,
            prepared.operation.upper(),
            prepared.request_payload,
            approval_use_id=prepared.approval_use_id,
            approval_digest=prepared.approval_digest,
            session_binding=prepared.session_binding,
            writer_lease=self._writer_lease,
            managed_intent_id=(
                prepared.managed_intent_id if prepared.operation == "submit" else None
            ),
            order_ref=prepared.order_ref if prepared.operation == "submit" else None,
            cancel_target=cancel_target,
            managed_action_id=(
                prepared.managed_cancel_intent_id
                if prepared.operation == "cancel"
                else prepared.managed_intent_id
            ),
            session_generation_id=prepared.session_generation_id,
            dispatch_front_id=prepared.dispatch_front_id,
            dispatch_session_id=prepared.dispatch_session_id,
            native_request_id=prepared.native_request_id,
            local_queue_receipt_id=prepared.local_queue_receipt_id,
            cancel_target_projection=cancel_target_projection,
            position_baseline_authorization=position_baseline_authorization,
            recovery_close_authorization=recovery_close_authorization,
        )
        return CtpManagedDispatchBinding.from_command(command, reservation)

    def _require_binding(self, command_id: str, binding: CtpManagedDispatchBinding) -> None:
        if type(binding) is not CtpManagedDispatchBinding or binding.command_id != command_id:
            raise ContractValidationError("typed managed CTP binding does not match command id")
        if not _HEX_32.fullmatch(binding.local_queue_receipt_id):
            raise ContractValidationError("managed CTP queue receipt ID is invalid")

    def _read_and_require_binding(
        self,
        command_id: str,
        binding: CtpManagedDispatchBinding,
        *,
        owner_handle: CtpCallbackSessionOwnerHandle | None = None,
        position_baseline_authorization: object | None = None,
        recovery_close_authorization: object | None = None,
    ) -> tuple[CtpDispatchCommand, CtpOrderIdentityReservation]:
        self._require_binding(command_id, binding)
        if position_baseline_authorization is not None or recovery_close_authorization is not None:
            if type(owner_handle) is not CtpCallbackSessionOwnerHandle:
                raise ContractValidationError(
                    "position dispatch authorization requires the exact callback session owner"
                )
            command = self._store.read_ctp_staged_command_for_session(
                self._scope,
                command_id,
                owner_handle=owner_handle,
                writer_lease=self._writer_lease,
                position_baseline_authorization=position_baseline_authorization,
                recovery_close_authorization=recovery_close_authorization,
            )
        else:
            command = self._store.read_ctp_dispatch_command(self._scope, command_id)
        if type(command) is not CtpDispatchCommand:
            raise ContractValidationError("managed CTP command is missing")
        reservation = self._store.read_ctp_order_identity(self._scope, binding.managed_intent_id)
        if type(reservation) is not CtpOrderIdentityReservation or not _binding_matches_command(
            binding, command, reservation
        ):
            raise ContractValidationError("managed CTP binding differs from durable command")
        return command, reservation


class CtpExecutionOwnerBindingMixin:
    """Validate CTP prepared actions and construct native dispatch receipts."""

    @staticmethod
    def _queue_receipt_values(
        prepared: CtpManagedPreparedDispatch, queue_receipt: Mapping[str, Any]
    ) -> tuple[str, bool]:
        if not isinstance(queue_receipt, Mapping):
            raise ContractValidationError("CTP local queue receipt mapping is required")
        if set(queue_receipt) != {"kind", "command", "receipt_id", "queued"}:
            raise ContractValidationError("CTP local queue receipt fields are not exact")
        receipt_id = queue_receipt.get("receipt_id")
        queued = queue_receipt.get("queued")
        if (
            queue_receipt.get("kind") != "command_receipt"
            or queue_receipt.get("command") != "submit"
            or type(receipt_id) is not str
            or receipt_id != prepared.local_queue_receipt_id
            or type(queued) is not bool
        ):
            raise ContractValidationError("CTP queue receipt differs from prepared SUBMIT")
        return receipt_id, queued

    @staticmethod
    def _cancel_queue_receipt_values(
        prepared: CtpManagedPreparedDispatch, queue_receipt: Mapping[str, Any]
    ) -> tuple[str, bool]:
        if not isinstance(queue_receipt, Mapping):
            raise ContractValidationError("CTP local queue receipt mapping is required")
        if set(queue_receipt) != {"kind", "command", "receipt_id", "queued"}:
            raise ContractValidationError("CTP local queue receipt fields are not exact")
        receipt_id = queue_receipt.get("receipt_id")
        queued = queue_receipt.get("queued")
        if (
            queue_receipt.get("kind") != "command_receipt"
            or queue_receipt.get("command") != "cancel"
            or type(receipt_id) is not str
            or receipt_id != prepared.local_queue_receipt_id
            or type(queued) is not bool
        ):
            raise ContractValidationError("CTP queue receipt differs from prepared CANCEL")
        return receipt_id, queued

    @staticmethod
    def _cancel_target_matches_prepared(
        prepared: CtpManagedPreparedDispatch,
        handle: CtpOrderTargetProjectionHandle,
        scope: ExecutionScope,
    ) -> bool:
        if (
            type(handle) is not CtpOrderTargetProjectionHandle
            or type(handle.projection) is not CtpVerifiedOrderTargetProjection
            or prepared.operation != "cancel"
        ):
            return False
        projection = handle.projection
        return (
            projection.account_key == scope.account_key
            and projection.scope_key == scope.key
            and projection.trading_day == scope.trading_day
            and projection.managed_intent_id == prepared.managed_intent_id
            and projection.runtime_order_id == prepared.runtime_order_id
            and projection.order_ref == prepared.order_ref
            and projection.exchange_id == prepared.cancel_target_exchange_id
            and projection.order_sys_id == prepared.cancel_target_order_sys_id
            and projection.front_id == prepared.cancel_target_front_id
            and projection.session_id == prepared.cancel_target_session_id
            and projection.session_generation_id == prepared.session_generation_id
        )

    @staticmethod
    def _prepared_matches_cancel_command(
        prepared: CtpManagedPreparedDispatch,
        binding: CtpManagedDispatchBinding,
        reservation: Any,
    ) -> bool:
        return (
            binding.operation == "cancel"
            and prepared.operation == binding.operation
            and prepared.command_id == binding.command_id
            and prepared.managed_intent_id == binding.managed_intent_id
            and prepared.runtime_order_id == binding.runtime_order_id
            and prepared.order_ref == binding.order_ref
            and dict(prepared.request_payload) == dict(binding.request_payload)
            and prepared.order_ref_reservation == reservation
            and prepared.managed_cancel_intent_id == binding.managed_action_id
            and prepared.cancel_target_exchange_id == binding.cancel_target_exchange_id
            and prepared.cancel_target_order_sys_id == binding.cancel_target_order_sys_id
            and prepared.cancel_target_front_id == binding.cancel_target_front_id
            and prepared.cancel_target_session_id == binding.cancel_target_session_id
            and prepared.approval_use_id == binding.approval_use_id
            and prepared.approval_digest == binding.approval_digest
            and dict(prepared.session_binding) == dict(binding.session_binding)
            and prepared.session_generation_id == binding.session_generation_id
            and prepared.dispatch_front_id == binding.dispatch_front_id
            and prepared.dispatch_session_id == binding.dispatch_session_id
            and prepared.native_request_id == binding.native_request_id
            and prepared.local_queue_receipt_id == binding.local_queue_receipt_id
        )

    @staticmethod
    def _prepared_matches_command(
        prepared: CtpManagedPreparedDispatch,
        binding: CtpManagedDispatchBinding,
        reservation: Any,
    ) -> bool:
        return (
            binding.operation == "submit"
            and prepared.operation == binding.operation
            and prepared.command_id == binding.command_id
            and prepared.managed_intent_id == binding.managed_intent_id
            and prepared.runtime_order_id == binding.runtime_order_id
            and prepared.order_ref == binding.order_ref
            and dict(prepared.request_payload) == dict(binding.request_payload)
            and prepared.order_ref_reservation == reservation
            and prepared.approval_use_id == binding.approval_use_id
            and prepared.approval_digest == binding.approval_digest
            and dict(prepared.session_binding) == dict(binding.session_binding)
            and prepared.session_generation_id == binding.session_generation_id
            and prepared.dispatch_front_id == binding.dispatch_front_id
            and prepared.dispatch_session_id == binding.dispatch_session_id
            and prepared.native_request_id == binding.native_request_id
            and prepared.local_queue_receipt_id == binding.local_queue_receipt_id
        )

    @staticmethod
    def _receipt(
        command: CtpDispatchCommand, outcome: str, payload: Mapping[str, Any]
    ) -> CtpDispatchReceipt:
        return CtpDispatchReceipt(
            receipt_type="ctp_dispatch_receipt.v2",
            command_id=command.command_id,
            account_key=command.account_key,
            scope_key=command.scope_key,
            trading_day=command.trading_day,
            operation=command.operation,
            request_payload_sha256=command.request_payload_sha256,
            reservation_managed_intent_id=command.reservation_managed_intent_id,
            order_ref=command.order_ref,
            cancel_target_order_ref=command.cancel_target_order_ref,
            cancel_target_exchange_id=command.cancel_target_exchange_id,
            cancel_target_order_sys_id=command.cancel_target_order_sys_id,
            cancel_target_front_id=command.cancel_target_front_id,
            cancel_target_session_id=command.cancel_target_session_id,
            approval_use_id=command.approval_use_id,
            approval_digest=command.approval_digest,
            session_binding_sha256=command.session_binding_sha256,
            outcome=outcome,
            native_receipt_payload=dict(payload),
            correlation_key=command.correlation_key,
            local_queue_receipt_id=command.local_queue_receipt_id,
        )
