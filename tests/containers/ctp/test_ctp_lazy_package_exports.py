from __future__ import annotations

import importlib
import subprocess
import sys
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[3]
SRC_ROOT = PACKAGE_ROOT / "src"

EXPECTED_EXPORTS = [
    "CTP_DIRECTION_MAP",
    "CTP_ORDER_STATUS_MAP",
    "CTP_POS_DIRECTION_MAP",
    "CtpAccountData",
    "CtpBarData",
    "CtpOrderData",
    "CtpPositionData",
    "CtpTickerData",
    "CtpTradeData",
]


def test_importing_ctp_package_does_not_eagerly_import_dto_or_optional_dependencies():
    script = f"""
import sys
sys.path.insert(0, {str(SRC_ROOT)!r})
import bt_api_ctp
import bt_api_ctp.containers.ctp as ctp_models

expected = {EXPECTED_EXPORTS!r}
assert ctp_models.__all__ == expected
assert set(expected).issubset(dir(ctp_models))
assert not any(name.startswith('bt_api_ctp.containers.ctp.ctp_') and name != 'bt_api_ctp.containers.ctp.ctp_native_query_certificate' for name in sys.modules)
assert 'bt_api_base.functions.utils' not in sys.modules
assert 'requests' not in sys.modules
assert 'dotenv' not in sys.modules
assert 'bt_api_ctp.ctp._ctp' not in sys.modules
try:
    ctp_models.not_a_public_export
except AttributeError:
    pass
else:
    raise AssertionError('unknown attribute was exposed')
"""
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-B", "-c", script],
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr


def test_all_public_ctp_exports_remain_lazy_direct_imports_with_stable_identity():
    package = importlib.import_module("bt_api_ctp.containers.ctp")
    expected_modules = {
        "CTP_DIRECTION_MAP": ".ctp_order",
        "CTP_ORDER_STATUS_MAP": ".ctp_order",
        "CTP_POS_DIRECTION_MAP": ".ctp_position",
        "CtpAccountData": ".ctp_account",
        "CtpBarData": ".ctp_bar",
        "CtpOrderData": ".ctp_order",
        "CtpPositionData": ".ctp_position",
        "CtpTickerData": ".ctp_ticker",
        "CtpTradeData": ".ctp_trade",
    }
    assert package.__all__ == EXPECTED_EXPORTS
    assert set(EXPECTED_EXPORTS).issubset(dir(package))

    resolved = {}
    for name, relative_module in expected_modules.items():
        value = getattr(package, name)
        source = importlib.import_module(relative_module, package.__name__)
        assert value is getattr(source, name)
        assert getattr(package, name) is value

        namespace = {}
        exec(
            f"from bt_api_ctp.containers.ctp import {name} as imported_value",
            namespace,
        )
        assert namespace["imported_value"] is value
        resolved[name] = value

    namespace = {}
    exec("from bt_api_ctp.containers.ctp import *", namespace)
    assert {name: namespace[name] for name in EXPECTED_EXPORTS} == resolved
