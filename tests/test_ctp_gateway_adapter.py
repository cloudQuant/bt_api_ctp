"""Tests for CTP gateway adapter field contracts."""

from __future__ import annotations

from typing import Any

from bt_api_ctp.gateway.adapter import CtpGatewayAdapter


class _FakeOrderRow:
    def get_order_id(self) -> str:
        return ''

    def get_client_order_id(self) -> str:
        return '000000123411'

    def get_order_exchange_id(self) -> str:
        return 'SHFE'

    @property
    def front_id(self) -> int:
        return 3

    @property
    def session_id(self) -> int:
        return 18472


class _FakePlaceOrderEnvelope:
    def get_status(self) -> bool:
        return True

    def get_data(self) -> list[Any]:
        return [self]

    def init_data(self) -> _FakeOrderRow:
        return _FakeOrderRow()


class _FakeCancelEnvelope:
    def get_status(self) -> bool:
        return True

    def get_data(self) -> list[dict[str, Any]]:
        return [
            {
                'OrderRef': '000000123411',
                'FrontID': 3,
                'SessionID': 18472,
                'ExchangeID': 'SHFE',
            }
        ]


class _FakeFeed:
    def make_order(self, *args: Any, **kwargs: Any) -> _FakePlaceOrderEnvelope:
        return _FakePlaceOrderEnvelope()

    def cancel_order(self, *args: Any, **kwargs: Any) -> _FakeCancelEnvelope:
        return _FakeCancelEnvelope()


class _FakeTraderClient:
    def get_session_state(self) -> dict[str, Any]:
        return {
            'auth_state': 'authenticated',
            'login_state': 'logged_in',
            'front_id': 7,
            'session_id': 8801,
            'trading_day': '20260618',
        }


class _FakeTradeStream:
    trader_client = _FakeTraderClient()


def test_get_session_state_forwards_underlying_trader_state() -> None:
    adapter = object.__new__(CtpGatewayAdapter)
    adapter.trade = _FakeTradeStream()

    state = adapter.get_session_state()

    assert state['auth_state'] == 'authenticated'
    assert state['login_state'] == 'logged_in'
    assert state['front_id'] == 7
    assert state['session_id'] == 8801
    assert state['trading_day'] == '20260618'


def test_place_order_does_not_promote_order_ref_to_external_id() -> None:
    """Pending CTP orders must not expose OrderRef as an exchange order id."""
    adapter = object.__new__(CtpGatewayAdapter)
    adapter.feed = _FakeFeed()
    adapter.last_price = {}

    result = adapter.place_order(
        {
            'symbol': 'rb2510.SHFE',
            'side': 'buy',
            'offset': 'open',
            'size': 1,
            'price': 3500.0,
            'client_order_id': 'client-1',
        }
    )

    assert result.get('id', '') == ''
    assert result['order_ref'] == '000000123411'
    assert result['order_id'] == ''
    assert result['external_order_id'] == ''
    assert result['order_sys_id'] == ''
    assert result['id_source'] == 'local_pending'


def test_cancel_order_does_not_promote_order_ref_to_external_id() -> None:
    """CTP cancel acknowledgements must keep OrderRef separate from OrderSysID."""
    adapter = object.__new__(CtpGatewayAdapter)
    adapter.feed = _FakeFeed()

    result = adapter.cancel_order(
        {
            'symbol': 'rb2510.SHFE',
            'order_ref': '000000123411',
            'front_id': 3,
            'session_id': 18472,
        }
    )

    assert result.get('id', '') == ''
    assert result['order_ref'] == '000000123411'
    assert result['external_order_id'] == ''
    assert result['order_sys_id'] == ''
    assert result['id_source'] == 'local_pending'
