"""CTP-owned selection of a verified connection pair for stream creation."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .ctp_env_selector import verify_official_simnow_profile


def verified_stream_configuration(feed: Any, parameters: Mapping[str, Any]) -> dict[str, str]:
    """Return a verified auto-selected pair; incomplete or custom pairs stay unpinned."""
    value = parameters.get("auto_detect_fronts")
    if not (
        value is True
        or (isinstance(value, str) and value.strip().lower() in {"1", "true", "yes", "on"})
    ):
        return {}
    if getattr(feed, "ctp_environment", None) != "simnow":
        return {}
    reader = getattr(feed, "get_environment_info", None)
    if not callable(reader):
        return {}
    try:
        info = reader()
    except Exception:
        return {}
    if (
        not isinstance(info, Mapping)
        or info.get("environment") != "demo"
        or info.get("verified") is not True
    ):
        return {}
    profile = str(getattr(feed, "ctp_env_profile", "") or "").strip()
    td_front = str(getattr(feed, "td_front", "") or "").strip()
    md_front = str(getattr(feed, "md_front", "") or "").strip()
    if (
        not profile
        or not td_front
        or not md_front
        or profile != str(info.get("profile") or "").strip()
    ):
        return {}
    try:
        verified = bool(verify_official_simnow_profile(td_front, md_front, profile))
    except Exception:
        return {}
    if not verified:
        return {}
    return {"ctp_env_profile": profile, "td_front": td_front, "md_front": md_front}


__all__ = ("verified_stream_configuration",)
