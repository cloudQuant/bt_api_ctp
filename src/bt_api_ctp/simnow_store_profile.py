"""Provider-owned validation for the official SimNow profile registry.

These names are direct aliases so callers use the same immutable registry
functions owned by the CTP SDK package. Importing this optional module is
explicit and does not select a front pair or create a provider session.
"""

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

from .ctp_env_selector import (
    official_simnow_fronts,
    official_simnow_profile_for_td_front,
    verify_official_simnow_profile,
)


_SIMNOW_PROFILE_ERROR_CODES = frozenset(
    {
        "CTP_CONFIG_MAPPINGS_REQUIRED",
        "CTP_FRONT_MISSING",
        "CTP_FRONT_INVALID",
        "CTP_PROFILE_INVALID",
        "CTP_PROFILE_CLAIMS_CONFLICT",
        "CTP_ENVIRONMENT_NOT_SIMNOW",
        "CTP_PROFILE_LOOKUP_UNAVAILABLE",
        "CTP_PROFILE_NOT_OFFICIAL_SIMNOW",
        "CTP_PROFILE_FRONT_MISMATCH",
    }
)


class SimNowProfileError(ValueError):
    """Finite, endpoint-free rejection of an unverified SimNow route config."""

    def __init__(self, code: str) -> None:
        self.code = code if type(code) is str and code in _SIMNOW_PROFILE_ERROR_CODES else (
            "CTP_PROFILE_NOT_OFFICIAL_SIMNOW"
        )
        super().__init__(self.code)


def _effective_front(options: Mapping[str, Any], address_key: str, front_key: str) -> str:
    value = options.get(address_key) or options.get(front_key)
    if value is None or value == "":
        raise SimNowProfileError("CTP_FRONT_MISSING")
    if type(value) is not str:
        raise SimNowProfileError("CTP_FRONT_INVALID")
    return value


def validate_official_simnow_store_profile(
    config: Mapping[str, Any], api_kwargs: Mapping[str, Any]
) -> Mapping[str, Any]:
    """Validate the effective SDK-owned CTP route settings against SimNow."""

    if not isinstance(config, Mapping) or not isinstance(api_kwargs, Mapping):
        raise SimNowProfileError("CTP_CONFIG_MAPPINGS_REQUIRED")
    options = dict(config)
    options.update(api_kwargs)
    td_front = _effective_front(options, "td_address", "td_front")
    md_front = _effective_front(options, "md_address", "md_front")

    claims = []
    for key in ("ctp_env_profile", "environment_profile"):
        if key not in options:
            continue
        value = options[key]
        if type(value) is not str or not value.strip():
            raise SimNowProfileError("CTP_PROFILE_INVALID")
        claims.append(value.strip().lower())
    if len(set(claims)) > 1:
        raise SimNowProfileError("CTP_PROFILE_CLAIMS_CONFLICT")
    profile = claims[0] if claims else ""

    for key in ("environment", "ctp_environment"):
        if key in options:
            value = options[key]
            if type(value) is not str or value.strip().lower() != "simnow":
                raise SimNowProfileError("CTP_ENVIRONMENT_NOT_SIMNOW")

    if not profile:
        profile = official_simnow_profile_for_td_front(td_front)
    if not profile:
        raise SimNowProfileError("CTP_PROFILE_NOT_OFFICIAL_SIMNOW")
    try:
        expected_td, expected_md = official_simnow_fronts(profile)
    except (KeyError, ValueError):
        raise SimNowProfileError("CTP_PROFILE_NOT_OFFICIAL_SIMNOW") from None
    if (
        td_front != expected_td
        or md_front != expected_md
        or not verify_official_simnow_profile(td_front, md_front, profile)
    ):
        raise SimNowProfileError("CTP_PROFILE_FRONT_MISMATCH")
    return MappingProxyType(
        {"verified": True, "environment": "simnow", "profile": profile}
    )

__all__ = [
    "official_simnow_fronts",
    "official_simnow_profile_for_td_front",
    "SimNowProfileError",
    "validate_official_simnow_store_profile",
    "verify_official_simnow_profile",
]
