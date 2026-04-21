"""Tests for exchange_registers/register_ctp.py."""

from __future__ import annotations

import pytest


class TestRegisterCtp:
    """Tests for CTP registration module."""

    def test_module_imports(self):
        """Test module can be imported."""
        try:
            from bt_api_ctp.registry_registration import register_ctp

            assert register_ctp is not None
        except ImportError:
            pytest.skip("CTP module not available")
