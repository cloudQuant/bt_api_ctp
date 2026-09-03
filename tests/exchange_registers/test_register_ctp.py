"""Tests for exchange_registers/register_ctp.py."""

from __future__ import annotations


class TestRegisterCtp:
    """Tests for CTP registration module."""

    def test_module_imports(self):
        """Test the actual CTP plugin registration entry point can be imported."""
        from bt_api_ctp.plugin import register_plugin

        assert register_plugin is not None
