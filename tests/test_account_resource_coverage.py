"""The public coverage declaration has no native-client import side effects."""
import subprocess
import sys


def test_coverage_contract_is_immutable_and_lightweight():
    script = """
import sys
from dataclasses import FrozenInstanceError
from bt_api_base.account_protocol import AccountResourceKey
from bt_api_ctp.account_resource_coverage import CTP_ACCOUNT_RESOURCE_COVERAGE
assert CTP_ACCOUNT_RESOURCE_COVERAGE == (AccountResourceKey('future', 'futures'),)
assert 'bt_api_ctp.ctp.client' not in sys.modules
try:
    CTP_ACCOUNT_RESOURCE_COVERAGE[0].margin_pool = 'main'
except FrozenInstanceError:
    pass
else:
    raise AssertionError('resource must be immutable')
"""
    subprocess.run([sys.executable, "-c", script], check=True)
