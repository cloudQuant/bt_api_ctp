"""Neutral account resources emitted by the CTP account observation provider.

This declaration describes projection coverage, not account authorization or
proof that an observation is complete or current.
"""

from bt_api_base.account_protocol import AccountResourceKey

CTP_ACCOUNT_RESOURCE_COVERAGE = (
    AccountResourceKey(product_type="future", margin_pool="futures"),
)
