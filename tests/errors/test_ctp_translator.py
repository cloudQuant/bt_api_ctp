"""Tests for CtpTranslator."""

from __future__ import annotations

from bt_api_ctp.errors.ctp_translator import CTPErrorTranslator


class TestCTPErrorTranslator:
    """Tests for CTPErrorTranslator."""

    def test_error_map_exists(self):
        """Test ERROR_MAP is defined."""
        assert hasattr(CTPErrorTranslator, "ERROR_MAP")
