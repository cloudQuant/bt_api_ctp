from __future__ import annotations

from importlib.util import find_spec
from pathlib import Path


def test_ctp_gateway_adapter_source_module_is_removed() -> None:
    package_root = Path(__file__).parents[1] / "src" / "bt_api_ctp" / "gateway"

    assert not (package_root / "__init__.py").exists()
    assert not (package_root / "adapter.py").exists()
    try:
        adapter_spec = find_spec("bt_api_ctp.gateway.adapter")
    except ModuleNotFoundError:
        adapter_spec = None
    assert adapter_spec is None


def test_retired_base_gateway_runtime_is_unavailable() -> None:
    try:
        runtime_spec = find_spec("bt_api_base.gateway.runtime")
    except ModuleNotFoundError:
        runtime_spec = None

    assert runtime_spec is None
