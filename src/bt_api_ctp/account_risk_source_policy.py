"""CTP's additional account-risk source binding requirement."""

from bt_api_base.account_risk_source_policy import AccountRiskSourcePolicy


def account_risk_source_policy():
    """Require the native account binding until a neutral proof is available."""
    return AccountRiskSourcePolicy(requires_native_account_binding=True)


__all__ = ("account_risk_source_policy",)
