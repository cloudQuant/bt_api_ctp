"""Tests for CTP module imports.

Note: CTP is a C++ extension module, tests verify module structure.
"""

from __future__ import annotations

import pytest


class TestCtpModule:
    """Tests for CTP module availability."""

    def test_ctp_constants_import(self):
        """Test ctp_constants can be imported."""
        try:
            from bt_api_ctp.ctp import ctp_constants

            assert ctp_constants is not None
        except ImportError:
            pytest.skip("CTP extension not available")

    def test_ctp_structs_common_import(self):
        """Test ctp_structs_common can be imported."""
        try:
            from bt_api_ctp.ctp import ctp_structs_common

            assert ctp_structs_common is not None
        except ImportError:
            pytest.skip("CTP extension not available")

    def test_ctp_native_diagnostics(self):
        """Native diagnostics describe the active Python/platform extension state."""
        from bt_api_ctp.ctp._ctp_base import (
            format_ctp_native_diagnostics,
            get_ctp_native_diagnostics,
        )

        diagnostics = get_ctp_native_diagnostics()
        assert isinstance(diagnostics["native_loaded"], bool)
        assert isinstance(diagnostics["expected_extensions"], list)
        assert isinstance(diagnostics["available_extensions"], list)
        assert diagnostics["python_version"]
        assert "CTP C++ extension (_ctp)" in format_ctp_native_diagnostics()
