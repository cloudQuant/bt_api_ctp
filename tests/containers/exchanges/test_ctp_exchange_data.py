"""Tests for CtpExchangeData container."""

from __future__ import annotations

from bt_api_ctp.exchange_data import CtpExchangeData


class TestCtpExchangeData:
    """Tests for CtpExchangeData."""

    def test_init(self):
        """Test initialization."""
        exchange = CtpExchangeData()

        assert exchange.exchange_name == "CTP"
