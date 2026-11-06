"""Bounded same-connection re-login after a transient CTP login refusal.

Iteration 62 (continuity-7, SimNow 2026-11-06): the native API reconnects on
its own and every ``OnFrontConnected`` already starts a new login.  A front
that keeps the TCP link open while it refuses logins (between sessions, before
the system is initialised for the next trading day) never raises another
``OnFrontConnected``, so one refused login left a long-running session logged
out for good.  This module holds the policy for retrying on the same
connection; the clients own their state and locking.

Only refusals that say "the front is not ready yet" are retried.  A credential
refusal (bad user or password, lockout, client authentication) is never
repeated on the same connection: repeating it can lock the account.  A login
answered with another account's identity is never retried either; it stays a
fail-closed rejection.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = (
    "MAX_RELOGIN_ATTEMPTS_PER_CONNECTION",
    "RELOGIN_BACKOFF_SECONDS",
    "TRANSIENT_LOGIN_ERROR_IDS",
    "TRANSIENT_SUBMIT_CODES",
    "LoginRefusal",
    "relogin_delay",
    "should_log_refusal",
    "transient_provider_refusal",
    "transient_submit_code",
)

# CTP error IDs (error.xml, API 6.7.7) that mean the front or the trading
# system is not ready: 1 INVALID_DATA_SYNC_STATUS, 7 NOT_INITED,
# 8 FRONT_NOT_ACTIVE, 35 BROKER_SYNCHRONIZING.  Everything else is final for
# the connection (for example 3 INVALID_LOGIN, 48 INVALID_INVESTORIDORPASSWORD,
# 63 AUTH_FAILED, 75 LOGIN_FORBIDDEN, 143 IP_FORBIDDEN).
TRANSIENT_LOGIN_ERROR_IDS = frozenset({1, 7, 8, 35})

# Native request submit codes: -1 network failure, -2 too many unhandled
# requests, -3 too many requests per second.  The request never reached the
# venue's credential check.
TRANSIENT_SUBMIT_CODES = frozenset({-1, -2, -3})

# Delay before the n-th consecutive retry on one connection (the last value
# is the cap).  The schedule reaches the cap after about two minutes.
RELOGIN_BACKOFF_SECONDS = (1.0, 2.0, 5.0, 10.0, 20.0, 30.0, 60.0)

# Retries per connection generation: at the 60 s cap this spans about six
# hours, longer than the gap between two CTP sessions on a trading day.
MAX_RELOGIN_ATTEMPTS_PER_CONNECTION = 360


@dataclass(frozen=True)
class LoginRefusal:
    """Value-free description of one refused login (never carries messages)."""

    reason: str
    retryable: bool
    connection_generation: int | None
    error_id: int | None = None


def transient_provider_refusal(error_id: object) -> bool:
    """Whether a venue error ID only says the front is not ready yet."""

    return type(error_id) is int and error_id in TRANSIENT_LOGIN_ERROR_IDS


def transient_submit_code(code: object) -> bool:
    """Whether a native request submit code never reached the venue."""

    return type(code) is int and code in TRANSIENT_SUBMIT_CODES


def relogin_delay(consecutive_failures: int) -> float | None:
    """Delay before the next retry, or ``None`` once the per-connection cap is hit."""

    if type(consecutive_failures) is not int or consecutive_failures < 1:
        return None
    if consecutive_failures > MAX_RELOGIN_ATTEMPTS_PER_CONNECTION:
        return None
    index = min(consecutive_failures, len(RELOGIN_BACKOFF_SECONDS)) - 1
    return RELOGIN_BACKOFF_SECONDS[index]


def should_log_refusal(consecutive_failures: int) -> bool:
    """Log the first refusals of a connection, then one in thirty (about 30 min)."""

    return consecutive_failures <= 3 or consecutive_failures % 30 == 0
