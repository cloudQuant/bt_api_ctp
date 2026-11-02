"""Validated CTP Trader/market front configuration; no connection is opened."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class CtpTraderConnectionConfig:
    """The explicit TD/MD front pair and credentials for one TraderClient."""

    front: str
    md_front: str
    broker_id: str
    user_id: str
    password: str = field(repr=False)
    ctp_env_profile: str = "config_front_pair"
    app_id: str = field(default="simnow_client_test", repr=False)
    auth_code: str = field(default="0000000000000000", repr=False)

    def __post_init__(self) -> None:
        for name in ("front", "md_front", "broker_id", "user_id", "password"):
            value = getattr(self, name)
            if type(value) is not str or not value or value != value.strip():
                raise ValueError(f"invalid CTP connection field: {name}")
        if (
            type(self.ctp_env_profile) is not str
            or not self.ctp_env_profile
            or self.ctp_env_profile != self.ctp_env_profile.strip()
        ):
            raise ValueError("invalid CTP environment profile")
        for name in ("app_id", "auth_code"):
            value = getattr(self, name)
            if type(value) is not str or not value:
                raise ValueError(f"invalid CTP connection field: {name}")


__all__ = ("CtpTraderConnectionConfig",)
