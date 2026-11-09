"""Gateway settlement source fencing with fake native requests only."""

import threading

import pytest

from bt_api_base.direct_write_protocol import (
    DirectWriteError, SettlementStatus, SettlementWriteBinding, WriteOperation,
)
from bt_api_ctp.direct_write_provider import CtpDirectWriteProvider
from bt_api_py.write_admission import WriteAdmission
from test_iter62_settlement_readback_on_write import TODAY, _login, _real_setup


@pytest.mark.parametrize("change", ["disconnect", "day", "account", "api", "spi"])
def test_gateway_observation_releases_provider_lock_and_rejects_changed_source(change):
    old_provider, client, native = _real_setup()
    old_provider.close()
    _login(client, native, session_id=20)
    native.confirmation_day = "20261105"
    feed = old_provider._feed
    entered = threading.Event()
    resume = threading.Event()
    result = []
    holder = {}
    original = native.ReqQrySettlementInfoConfirm

    def blocked(field, request_id):
        entered.set()
        if not resume.wait(5):
            raise RuntimeError("synthetic native timeout")
        return original(field, request_id)

    native.ReqQrySettlementInfoConfirm = blocked

    def run():
        provider = CtpDirectWriteProvider(
            feed, owner_thread_only=True, background_settlement_readback=False,
            allow_implicit_connect=False,
        )
        holder["provider"] = provider
        try:
            result.append(provider.observe_settlement())
        finally:
            provider.close()

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    try:
        assert entered.wait(3)
        provider = holder["provider"]
        assert provider._lock.acquire(timeout=0.5)
        provider._lock.release()
        if change == "disconnect":
            client._on_front_disconnected(1)
        else:
            with client._query_state_lock:
                if change == "day":
                    client._trading_day = "20261109"
                elif change == "account":
                    client._account_fingerprint = "different"
                elif change == "api":
                    client._api = object()
                else:
                    client._spi = object()
    finally:
        resume.set()
        worker.join(5)
    assert not worker.is_alive()
    assert result == [None]
    assert "confirm_settlement" not in native.calls


@pytest.mark.parametrize("day,status", [
    ("20261105", SettlementStatus.UNCONFIRMED),
    (TODAY, SettlementStatus.CONFIRMED),
])
def test_gateway_observation_keeps_exact_source_ticket(day, status):
    old_provider, client, native = _real_setup()
    old_provider.close()
    _login(client, native, session_id=20)
    native.confirmation_day = day
    provider = CtpDirectWriteProvider(
        old_provider._feed, owner_thread_only=True, background_settlement_readback=False,
        allow_implicit_connect=False,
    )
    try:
        observation = provider.observe_settlement()
        assert observation is not None
        assert observation.status is status
        assert provider.validate_settlement_observation(observation)
    finally:
        provider.close()


def test_gateway_reentrant_observation_never_issues_second_query():
    old_provider, client, native = _real_setup()
    old_provider.close()
    _login(client, native, session_id=20)
    native.confirmation_day = "20261105"
    provider = CtpDirectWriteProvider(
        old_provider._feed, owner_thread_only=True, background_settlement_readback=False,
        allow_implicit_connect=False,
    )
    original = native.ReqQrySettlementInfoConfirm
    inner = []

    def read(field, request_id):
        inner.append(provider.observe_settlement())
        return original(field, request_id)

    native.ReqQrySettlementInfoConfirm = read
    try:
        observation = provider.observe_settlement()
        assert observation is not None
        assert inner == [None]
        assert native.calls.count("query_settlement") == 1
        assert provider.validate_settlement_observation(observation)
    finally:
        provider.close()


def test_gateway_confirmation_releases_provider_lock_over_native_and_consumes_grant():
    """Decision A: the Gateway owner already holds the marker and its claim.

    The provider validates and consumes the grant under its lock, then calls
    native without holding it; the observation ticket is single use.
    """
    import threading
    from types import SimpleNamespace

    from bt_api_base.direct_write_protocol import SettlementStatus, WriteAdmissionDenied

    old_provider, client, native = _real_setup()
    old_provider.close()
    _login(client, native, session_id=20)
    native.confirmation_day = "20261105"
    provider = CtpDirectWriteProvider(
        old_provider._feed, owner_thread_only=True, background_settlement_readback=False,
        allow_implicit_connect=False,
    )
    # Same stand-in for the feed's SDK admission step as the helper setup.
    provider._admitted_capability = old_provider._admitted_capability
    seen = {}

    def confirm(_field, request_id):
        native.calls.append("confirm_settlement")
        acquired = []

        def probe():
            ok = provider._lock.acquire(timeout=1.0)
            if ok:
                provider._lock.release()  # an RLock must be released by its owner
            acquired.append(ok)

        worker = threading.Thread(target=probe)
        worker.start()
        worker.join(2.0)
        seen["provider_lock_free"] = acquired == [True]
        seen["inflight"] = provider._gateway_settlement_inflight is not None
        native.confirmation_day = TODAY
        client._spi.OnRspSettlementInfoConfirm(
            SimpleNamespace(BrokerID="9999", InvestorID="account", ConfirmDate=TODAY),
            SimpleNamespace(ErrorID=0, ErrorMsg=""), request_id, True)
        return 0

    native.ReqSettlementInfoConfirm = confirm
    try:
        observation = provider.observe_settlement()
        binding = SettlementWriteBinding(observation, "synthetic-marker")
        admission = WriteAdmission({"CTP___FUTURE": True})
        grant = admission.issue(
            "CTP___FUTURE", WriteOperation.SETTLEMENT_CONFIRM,
            observation.session_generation, settlement_binding=binding,
        )
        status = provider.confirm_settlement(grant, admission, observation=observation)
        assert status is SettlementStatus.CONFIRMED
        assert native.calls.count("confirm_settlement") == 1
        assert seen == {"provider_lock_free": True, "inflight": True}
        assert provider._gateway_settlement_inflight is None
        with pytest.raises(WriteAdmissionDenied):
            admission.consume(
                grant, exchange_name="CTP___FUTURE", operation=WriteOperation.SETTLEMENT_CONFIRM,
                session_generation=observation.session_generation, settlement_binding=binding,
            )
        again = admission.issue(
            "CTP___FUTURE", WriteOperation.SETTLEMENT_CONFIRM,
            observation.session_generation, settlement_binding=binding,
        )
        with pytest.raises(DirectWriteError, match="ctp_settlement_observation_invalid"):
            provider.confirm_settlement(again, admission, observation=observation)
        assert native.calls.count("confirm_settlement") == 1
    finally:
        provider.close()
