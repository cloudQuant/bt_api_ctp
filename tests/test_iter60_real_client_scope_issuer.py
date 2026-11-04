"""Iteration 60: the read-only CTP account provider trusts a real TraderClient's scope.

A real ``TraderClient`` seals session scopes and query sources with a private
issuer object, not with itself. The provider used to require ``issuer is
trader`` and so refused every real client. Offline: no native library or front.
"""

from __future__ import annotations

from bt_api_ctp.account_provider import _issued_by
from bt_api_ctp.ctp.client import TraderClient


def _client():
    client = object.__new__(TraderClient)
    client._query_evidence_issuer = object()
    return client


def test_a_real_client_scope_issuer_is_trusted_by_identity_only():
    client = _client()
    assert _issued_by(client, client._query_evidence_issuer) is True
    assert _issued_by(client, client) is True  # a trader that issues as itself
    assert _issued_by(client, object()) is False
    assert _issued_by(client, _client()._query_evidence_issuer) is False  # another client


def test_an_object_without_the_predicate_is_only_trusted_as_itself():
    trader = object()
    assert _issued_by(trader, trader) is True
    assert _issued_by(trader, object()) is False
