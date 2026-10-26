# I51 CTP no-Join source review

**Result: GO — independent static source and test review only.** No tests, imports, SDK/native calls, provider access, or private-input reads were performed in this review.

Reviewed public SDK source `D:/bt_api_py/bt_api/bt_api_ctp/src/bt_api_ctp/ctp/client.py` — 494,245 bytes, SHA-256 `ab11ec060c5f447e1fb70ce462f4add2753cf6b9c5c017b1a1b6bf3fa97518b8` — and focused test source `D:/bt_api_py/bt_api/bt_api_ctp/tests/test_ctp_join_observer_policy.py` — final author pin 26,471 bytes, SHA-256 `6a1e1a26464d81e1385822f708bdbee88a3916e9e78006153ce1eea198245ffc`. This final test pin removes one unused local assignment from the reviewed file; the author reports the two affected focused nodes passed against the unchanged source pin. The prior reviewed test pin was 26,518 bytes, SHA-256 `ba801f6e409c126c96e9488849367e3931db5bd7af5294d30bed31662f5929e2`.

The public API keeps `join_observer=True` by default and retains the existing positional `block` argument; opt-out is keyword-only and rejects blocking mode. The no-Join policy is bound to the exact API object. Deferred cleanup checks startup, request, and callback leases; one-time release fencing prevents duplicate Release; and uncertain or pending cleanup remains incomplete. Static test coverage includes both MD and Trader clients, default compatibility, concurrent stop, startup and SPI-construction failures, callback/request drains, blocked and failed Release, deferred-owner receipt completion, and cleanup-worker startup failure. The author reports 35 focused passes; that result was not independently executed here.

The implementation corrected three review findings before freeze: (1) stop receipts now wait for both Release confirmation and clearance of the exact API's deferred owner record; (2) MD no-Join cleanup after a non-Init startup call now uses lease-gated deferred cleanup, including outstanding callback leases; and (3) Trader cleanup-worker startup failure is contained without escaping the SDK callback or falling back to Join/Release.

One scope limitation remains: `OneShotMdDiagnosticClient.start(self, block=False)` does not expose `join_observer`, so that specialized subclass cannot opt in. This does not change its existing default behavior or block the reviewed base MD/Trader lifecycle path.

This review establishes source and fake-test structure only. It does not establish installed-artifact, native SDK lifecycle, provider, account, or formal acceptance.
