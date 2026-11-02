"""Exact-route registration for CTP instrument execution-rules snapshots."""

from __future__ import annotations

from typing import Optional

from bt_api_base.execution_correlation_protocol import SelectedExecutionRoute
from bt_api_base.execution_rules_protocol import (
    ExecutionRulesSnapshotPort,
    ExecutionRulesSnapshotProviderRegistryPort,
)

from bt_api_ctp.execution_economics_registration import (
    CtpExecutionEconomicsOwner,
    CtpExecutionRulesSnapshotEvidenceIssuer,
)

__all__ = ["register_ctp_execution_rules_snapshot_provider"]

_CTP_FUTURE_ROUTE = "CTP___FUTURE"


def _create_ctp_execution_rules_snapshot_owner(
    route: SelectedExecutionRoute,
) -> Optional[ExecutionRulesSnapshotPort]:
    """Return the exact runtime-sealed CTP owner only when its issuer is capable."""

    if type(route) is not SelectedExecutionRoute or route.exchange_name != _CTP_FUTURE_ROUTE:
        return None
    try:
        from bt_api_ctp.order_provider import CtpOrderProviderRuntimeContext

        context = route.execution_context
        if type(context) is not CtpOrderProviderRuntimeContext:
            return None
        owner = context.execution_economics_owner
        issuer = context.execution_economics_issuer
        if (
            type(owner) is not CtpExecutionEconomicsOwner
            or owner._issuer is not issuer
            or not isinstance(issuer, CtpExecutionRulesSnapshotEvidenceIssuer)
            or not isinstance(owner, ExecutionRulesSnapshotPort)
            or owner._bind_selected_route(route, context) is not True
        ):
            return None
        return owner
    except Exception:
        return None


def register_ctp_execution_rules_snapshot_provider(
    registry: ExecutionRulesSnapshotProviderRegistryPort,
) -> None:
    """Register only the SDK-selected CTP futures route."""

    if not isinstance(registry, ExecutionRulesSnapshotProviderRegistryPort):
        raise TypeError("registry must satisfy ExecutionRulesSnapshotProviderRegistryPort")
    registry.register(_CTP_FUTURE_ROUTE, _create_ctp_execution_rules_snapshot_owner)
