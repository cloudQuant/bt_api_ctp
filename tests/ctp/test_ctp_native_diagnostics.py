"""Tests for portable CTP native-runtime diagnostics."""

from __future__ import annotations

from bt_api_ctp.ctp import _ctp_base


def test_native_diagnostics_describe_the_current_interpreter_and_runtime():
    """A missing native module has actionable platform and ABI diagnostics."""
    diagnostics = _ctp_base.get_ctp_native_diagnostics()

    assert diagnostics['native_loaded'] is _ctp_base.is_ctp_native_loaded()
    assert diagnostics['reason'] in {
        'native_loaded',
        'missing_extension_for_platform',
        'matching_extension_failed_to_load',
    }
    assert diagnostics['expected_extensions']
    assert isinstance(diagnostics['available_extensions'], list)
    assert _ctp_base.format_ctp_native_diagnostics()
