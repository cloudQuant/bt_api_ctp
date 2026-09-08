"""Fail-closed SimNow front selection with explicit readiness evidence."""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from datetime import datetime, time
from typing import Any

_TRADING_SESSIONS = (
    (time(9, 0), time(11, 30)),
    (time(13, 30), time(15, 0)),
    (time(21, 0), time(23, 59, 59)),
)
_NIGHT_SESSION_AFTER_MIDNIGHT = (time(0, 0), time(2, 30))
_SET1_DEFAULTS = {
    "1": ("tcp://180.168.146.187:10201", "tcp://180.168.146.187:10211"),
    "2": ("tcp://180.168.146.187:10202", "tcp://180.168.146.187:10212"),
}
_SET2_DEFAULT = ("tcp://180.168.146.187:10130", "tcp://180.168.146.187:10131")


@dataclass(frozen=True)
class CtpEnvironmentSelection:
    td_front: str
    md_front: str
    environment: str
    profile: str
    readiness: str
    reason: str
    calendar_verified: bool
    explicit: bool

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _in_trading_session(now: datetime) -> bool:
    current = now.time()
    if _NIGHT_SESSION_AFTER_MIDNIGHT[0] <= current <= _NIGHT_SESSION_AFTER_MIDNIGHT[1]:
        return True
    return any(start <= current <= end for start, end in _TRADING_SESSIONS)


def _get_set1_selection(
    *, explicit: bool, calendar_verified: bool
) -> CtpEnvironmentSelection:
    group = str(os.environ.get("CTP_SET1_GROUP") or "1").strip()
    if group not in _SET1_DEFAULTS:
        raise ValueError(f"unsupported CTP_SET1_GROUP {group!r}; expected '1' or '2'")
    default_td, default_md = _SET1_DEFAULTS[group]
    td = str(os.environ.get(f"CTP_SET1_TD_FRONT_{group}") or default_td).strip()
    md = str(os.environ.get(f"CTP_SET1_MD_FRONT_{group}") or default_md).strip()
    if not td or not md:
        raise ValueError("selected SimNow set1 profile has an empty front")
    official = (td, md) == (default_td, default_md)
    return CtpEnvironmentSelection(
        td_front=td,
        md_front=md,
        environment="simnow" if official else "custom",
        profile=f"set1_group{group}",
        readiness=(
            ("profile_selected" if explicit else "calendar_verified_session")
            if official
            else "custom_front_override"
        ),
        reason="explicit_set1" if explicit else "verified_trading_day_and_session",
        calendar_verified=calendar_verified,
        explicit=explicit,
    )


def _get_set2_selection(
    *, explicit: bool, calendar_verified: bool
) -> CtpEnvironmentSelection:
    td = str(os.environ.get("CTP_SET2_TD_FRONT") or _SET2_DEFAULT[0]).strip()
    md = str(os.environ.get("CTP_SET2_MD_FRONT") or _SET2_DEFAULT[1]).strip()
    if not td or not md:
        raise ValueError("selected SimNow set2 profile has an empty front")
    official = (td, md) == _SET2_DEFAULT
    if explicit:
        readiness = "profile_selected"
        reason = "explicit_set2"
    elif calendar_verified:
        readiness = "calendar_verified_off_session"
        reason = "verified_non_trading_day_or_off_session"
    else:
        readiness = "calendar_unverified"
        reason = "auto_requires_exchange_calendar_for_set1"
    if not official:
        readiness = "custom_front_override"
    return CtpEnvironmentSelection(
        td_front=td,
        md_front=md,
        environment="simnow" if official else "custom",
        profile="set2_7x24",
        readiness=readiness,
        reason=reason,
        calendar_verified=calendar_verified,
        explicit=explicit,
    )


def select_ctp_environment(
    env: str = "",
    now: datetime | None = None,
    *,
    is_trading_day: bool | None = None,
    require_profile: str | None = None,
) -> CtpEnvironmentSelection:
    """Select a profile without pretending weekdays are an exchange calendar.

    ``auto`` only selects set1 when the caller supplies verified trading-day
    evidence and the time is in a configured session. Without that evidence it
    selects the 7x24 profile and marks the result ``calendar_unverified``.
    """
    selected_env = str(env or os.environ.get("CTP_ENV") or "auto").strip().lower()
    if selected_env not in {"auto", "set1", "set2"}:
        raise ValueError(
            f"unsupported CTP_ENV {selected_env!r}; expected auto, set1, or set2"
        )
    current = now or datetime.now()
    if selected_env == "set1":
        selection = _get_set1_selection(
            explicit=True, calendar_verified=is_trading_day is True
        )
    elif selected_env == "set2":
        selection = _get_set2_selection(
            explicit=True, calendar_verified=is_trading_day is not None
        )
    elif is_trading_day is True and _in_trading_session(current):
        selection = _get_set1_selection(explicit=False, calendar_verified=True)
    else:
        selection = _get_set2_selection(
            explicit=False, calendar_verified=is_trading_day is not None
        )

    required = str(require_profile or "").strip().lower()
    if required:
        matches = (
            selection.profile == required
            or (required == "set1" and selection.profile.startswith("set1_"))
            or (required == "set2" and selection.profile.startswith("set2_"))
        )
        if not matches:
            raise RuntimeError(
                f"required CTP profile {required!r}, selected {selection.profile!r} "
                f"({selection.readiness})"
            )
    return selection


def verify_official_simnow_profile(td_front: str, md_front: str, profile: str) -> bool:
    """Verify that a claimed profile uses its current official endpoint pair."""
    name = str(profile or "").strip().lower()
    expected = {
        "set1_group1": _SET1_DEFAULTS["1"],
        "set1_group2": _SET1_DEFAULTS["2"],
        "set2_7x24": _SET2_DEFAULT,
    }.get(name)
    return (
        expected is not None
        and (str(td_front).strip(), str(md_front).strip()) == expected
    )


def official_simnow_fronts(profile: str) -> tuple[str, str]:
    """Return the immutable official endpoint pair for a named profile."""
    name = str(profile or "").strip().lower()
    expected = {
        "set1_group1": _SET1_DEFAULTS["1"],
        "set1_group2": _SET1_DEFAULTS["2"],
        "set2_7x24": _SET2_DEFAULT,
    }.get(name)
    if expected is None:
        raise ValueError(f"unsupported CTP profile {profile!r}")
    return expected


def get_ctp_fronts(
    env: str = "",
    now: datetime | None = None,
    *,
    is_trading_day: bool | None = None,
    require_profile: str | None = None,
) -> tuple[str, str, str]:
    selection = select_ctp_environment(
        env,
        now,
        is_trading_day=is_trading_day,
        require_profile=require_profile,
    )
    return selection.td_front, selection.md_front, selection.profile


def apply_ctp_env(**kwargs: Any) -> tuple[str, str, str]:
    selection = select_ctp_environment(**kwargs)
    os.environ["CTP_TD_FRONT"] = selection.td_front
    os.environ["CTP_MD_FRONT"] = selection.md_front
    return selection.td_front, selection.md_front, selection.profile


__all__ = [
    "CtpEnvironmentSelection",
    "apply_ctp_env",
    "get_ctp_fronts",
    "official_simnow_fronts",
    "select_ctp_environment",
    "verify_official_simnow_profile",
]
