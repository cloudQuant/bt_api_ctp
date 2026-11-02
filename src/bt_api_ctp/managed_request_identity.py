"""CTP managed request identity values; these never grant dispatch authority."""
from __future__ import annotations

import re
import sys
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import date
from typing import Any

_DATACLASS_SLOT_OPTIONS = {"slots": True} if sys.version_info >= (3, 10) else {}


@dataclass(frozen=True, **_DATACLASS_SLOT_OPTIONS)
class CtpOrderIdentityBinding:
    """Non-authorizing I9 identity labels attached to a managed CTP order.

    This value lets the SDK compare a request with an already mirrored
    reservation. It is not an approval, provider receipt, or dispatch
    capability.
    """

    environment: str
    account_key: str
    trading_day: str
    scope_key: str
    managed_intent_id: str
    runtime_order_id: str

    def __post_init__(self) -> None:
        if (
            type(self.environment) is not str
            or not self.environment
            or self.environment != self.environment.strip()
        ):
            raise ValueError("CTP identity environment must be a non-empty trimmed string")
        if (
            type(self.account_key) is not str
            or re.fullmatch(r"account:[0-9a-f]{64}", self.account_key, re.ASCII) is None
        ):
            raise ValueError("CTP identity account_key must be an account SHA-256 key")
        if (
            type(self.trading_day) is not str
            or re.fullmatch(r"[0-9]{8}", self.trading_day, re.ASCII) is None
        ):
            raise ValueError("CTP identity trading_day must use YYYYMMDD")
        try:
            date(
                int(self.trading_day[:4]),
                int(self.trading_day[4:6]),
                int(self.trading_day[6:8]),
            )
        except ValueError as error:
            raise ValueError("CTP identity trading_day must be a calendar date") from error
        if (
            type(self.scope_key) is not str
            or re.fullmatch(r"scope:[0-9a-f]{64}", self.scope_key, re.ASCII) is None
        ):
            raise ValueError("CTP identity scope_key must be a scope SHA-256 key")
        if (
            type(self.managed_intent_id) is not str
            or re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", self.managed_intent_id, re.ASCII) is None
        ):
            raise ValueError("CTP identity managed_intent_id is invalid")
        if (
            type(self.runtime_order_id) is not str
            or re.fullmatch(r"bt-managed-v1:[0-9a-f]{64}", self.runtime_order_id, re.ASCII) is None
        ):
            raise ValueError("CTP identity runtime_order_id is invalid")

    @property
    def dispatch_authorized(self) -> bool:
        """Identity comparison never grants dispatch authority."""
        return False

    def to_dict(self) -> dict[str, str]:
        """Return the stable mapping form used inside typed order requests."""
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> CtpOrderIdentityBinding:
        """Rebuild and validate the exact nested identity mapping."""
        fields = {
            "environment",
            "account_key",
            "trading_day",
            "scope_key",
            "managed_intent_id",
            "runtime_order_id",
        }
        if not isinstance(value, Mapping) or set(value) != fields:
            raise ValueError("CTP identity mapping has missing or unknown fields")
        return cls(**dict(value))


@dataclass(frozen=True, **_DATACLASS_SLOT_OPTIONS)
class CtpCancelIdentityBinding:
    """Versioned, non-authorizing echo of one I9 CTP cancel command.

    This binds a typed request to the exact shared I9 command and its complete
    CTP target. It is not a fresh approval, a claimed-row capability, a local
    queue receipt, or permission to call a native client. The public cancel
    path remains closed until those separate handoffs are implemented.
    """

    version: int
    environment: str
    account_id: str
    instrument_id: str
    account_key: str
    trading_day: str
    scope_key: str
    managed_intent_id: str
    runtime_order_id: str
    order_ref: str
    command_id: str
    request_payload_sha256: str
    managed_action_id: str
    approval_use_id: str
    approval_digest: str
    session_binding_sha256: str
    session_generation_id: str
    dispatch_front_id: int
    dispatch_session_id: int
    native_request_id: int
    native_action_ref: str | int
    cancel_target_order_ref: str
    cancel_target_exchange_id: str
    cancel_target_order_sys_id: str
    cancel_target_front_id: int
    cancel_target_session_id: int
    native_request_payload_sha256: str | None = None

    def __post_init__(self) -> None:
        if type(self.version) is not int or self.version not in {1, 2}:
            raise ValueError("unsupported CTP cancel identity version")
        for name in (
            "environment",
            "account_id",
            "instrument_id",
            "command_id",
            "managed_action_id",
            "approval_use_id",
            "session_generation_id",
        ):
            value = getattr(self, name)
            if (
                type(value) is not str
                or not value
                or value != value.strip()
                or len(value) > 256
                or not value.isascii()
            ):
                raise ValueError("invalid CTP cancel identity " + name)
        for name in ("account_key", "scope_key"):
            value = getattr(self, name)
            prefix = "account:" if name == "account_key" else "scope:"
            if (
                type(value) is not str
                or re.fullmatch(prefix + r"[0-9a-f]{64}", value, re.ASCII) is None
            ):
                raise ValueError("invalid CTP cancel identity " + name)
        if (
            type(self.trading_day) is not str
            or re.fullmatch(r"[0-9]{8}", self.trading_day, re.ASCII) is None
        ):
            raise ValueError("invalid CTP cancel identity trading_day")
        try:
            date(
                int(self.trading_day[:4]),
                int(self.trading_day[4:6]),
                int(self.trading_day[6:8]),
            )
        except ValueError as error:
            raise ValueError("invalid CTP cancel identity trading_day") from error
        if (
            type(self.managed_intent_id) is not str
            or re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", self.managed_intent_id, re.ASCII) is None
        ):
            raise ValueError("invalid CTP cancel identity managed_intent_id")
        if (
            type(self.runtime_order_id) is not str
            or re.fullmatch(r"bt-managed-v1:[0-9a-f]{64}", self.runtime_order_id, re.ASCII) is None
        ):
            raise ValueError("invalid CTP cancel identity runtime_order_id")
        for name in (
            "request_payload_sha256",
            "approval_digest",
            "session_binding_sha256",
        ):
            if (
                type(getattr(self, name)) is not str
                or re.fullmatch(r"[0-9a-f]{64}", getattr(self, name), re.ASCII) is None
            ):
                raise ValueError("invalid CTP cancel identity " + name)
        for name, value in (
            ("order_ref", self.order_ref),
            ("cancel_target_order_ref", self.cancel_target_order_ref),
        ):
            if type(value) is not str or re.fullmatch(r"[0-9]{12}", value, re.ASCII) is None:
                raise ValueError("invalid CTP cancel identity " + name)
        if self.order_ref != self.cancel_target_order_ref:
            raise ValueError("CTP cancel identity target OrderRef differs from reservation")
        if (
            re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", self.managed_action_id, re.ASCII) is None
            or self.managed_action_id == self.managed_intent_id
        ):
            raise ValueError("CTP cancel action id is invalid or equals managed intent id")
        if (
            type(self.cancel_target_exchange_id) is not str
            or not 1 <= len(self.cancel_target_exchange_id) <= 9
            or not self.cancel_target_exchange_id.isascii()
            or not self.cancel_target_exchange_id.isalnum()
        ):
            raise ValueError("invalid CTP cancel identity cancel_target_exchange_id")
        if (
            type(self.cancel_target_order_sys_id) is not str
            or not 1 <= len(self.cancel_target_order_sys_id) <= 21
            or not self.cancel_target_order_sys_id.isascii()
            or self.cancel_target_order_sys_id != self.cancel_target_order_sys_id.strip()
        ):
            raise ValueError("invalid CTP cancel identity cancel_target_order_sys_id")
        for name in (
            "dispatch_front_id",
            "dispatch_session_id",
            "native_request_id",
            "cancel_target_front_id",
            "cancel_target_session_id",
        ):
            value = getattr(self, name)
            if type(value) is not int or not 0 < value <= 2_147_483_647:
                raise ValueError("invalid CTP cancel identity " + name)
        if self.version == 1:
            if (
                self.native_request_payload_sha256 is not None
                or type(self.native_action_ref) is not str
                or re.fullmatch(r"[0-9]{1,12}", self.native_action_ref, re.ASCII) is None
                or int(self.native_action_ref) != self.native_request_id
            ):
                raise ValueError("invalid legacy CTP cancel identity native action reference")
        elif (
            type(self.native_action_ref) is not int
            or not 1 <= self.native_action_ref <= 2_147_483_647
            or type(self.native_request_payload_sha256) is not str
            or re.fullmatch(r"[0-9a-f]{64}", self.native_request_payload_sha256, re.ASCII) is None
        ):
            raise ValueError("invalid CTP cancel identity v2 native payload binding")

    @property
    def dispatch_authorized(self) -> bool:
        """An identity echo never grants local dispatch authority."""
        return False

    @property
    def runtime_action_id(self) -> str:
        """Return the I9 managed action identity expected by the SDK seam."""
        return self.managed_action_id

    @property
    def managed_cancel_intent_id(self) -> str:
        """Return the I9 managed action identity expected by the SDK seam."""
        return self.managed_action_id

    def to_dict(self) -> dict[str, Any]:
        """Return the exact JSON-compatible nested mapping."""
        value = asdict(self)
        if self.version == 1:
            value.pop("native_request_payload_sha256")
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> CtpCancelIdentityBinding:
        """Rebuild an exact v1 or v2 mapping, rejecting unknown or missing keys."""
        fields = {
            "version",
            "environment",
            "account_id",
            "instrument_id",
            "account_key",
            "trading_day",
            "scope_key",
            "managed_intent_id",
            "runtime_order_id",
            "order_ref",
            "command_id",
            "request_payload_sha256",
            "managed_action_id",
            "approval_use_id",
            "approval_digest",
            "session_binding_sha256",
            "session_generation_id",
            "dispatch_front_id",
            "dispatch_session_id",
            "native_request_id",
            "native_action_ref",
            "cancel_target_order_ref",
            "cancel_target_exchange_id",
            "cancel_target_order_sys_id",
            "cancel_target_front_id",
            "cancel_target_session_id",
        }
        v2_fields = fields | {"native_request_payload_sha256"}
        if not isinstance(value, Mapping):
            raise ValueError("CTP cancel identity mapping has missing or unknown fields")
        expected_fields = fields if value.get("version") == 1 else v2_fields
        if set(value) != expected_fields:
            raise ValueError("CTP cancel identity mapping has missing or unknown fields")
        return cls(**dict(value))


