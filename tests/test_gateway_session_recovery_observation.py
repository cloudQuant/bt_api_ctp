"""The Gateway session forwards only the explicit local observation capacity."""
import os

import pytest
from bt_api_base.account_protocol import AccountKey
from bt_api_base.market_recovery_observation import RecoveryObservationPage

from bt_api_ctp.gateway_session import CtpGatewaySession
from bt_api_ctp.market_provider import CtpQuoteV2MarketProviderError


def test_session_constructs_real_provider_with_optional_observation_buffer(tmp_path):
    root = tmp_path / 'flow'
    root.mkdir(mode=0o700)
    key = AccountKey('ctp', 'simnow', 'simnow', 'account', '9999')
    options = dict(trusted_flow_root=root, flow_account_scope=key.account_key_id,
                   source_ref='source', entitlement_ref='entitlement')
    disabled = CtpGatewaySession(key, lambda: None, lambda *_: None, **options)
    with pytest.raises(CtpQuoteV2MarketProviderError, match='RECOVERY_OBSERVATION_DISABLED'):
        disabled._market.read_recovery_observations(after_sequence=0, limit=4)
    enabled = CtpGatewaySession(key, lambda: None, lambda *_: None,
                                recovery_observation_capacity=4, **options)
    page = enabled._market.read_recovery_observations(after_sequence=0, limit=4)
    assert type(page) is RecoveryObservationPage
    assert page.owner_pid == os.getpid() and page.after_sequence == 0
