# G5 I13 source API compatibility inventory

Status: `SOURCE_ONLY / FAKE_ONLY / NO_WHEEL / NO_PIN / NATIVE_UNVERIFIED`.

This inventory compares the isolated I9+Join candidate with the current dirty
I13 SDK snapshot and the checked-in Backtrader/I9 consumers. It records source
contracts and missing integration work; test overlays and copied suites are
not package compatibility evidence.

## Source provenance

| Source | Revision / state | Relevant current SHA-256 |
|---|---|---|
| SDK candidate | `D:\bt_api_ctp_i13_g5_manual_candidate_20260926`, base `9976bcbbbe331ee77e2d90e05da08472259a625a`, dirty manual changes | See the candidate file hashes below. |
| I13 parent | `D:\bt_api_py`, HEAD `d3674e19a11b9f35f19ae756899bcf18854c8c46`, dirty submodule pointer | Read only. |
| I13 SDK submodule | `D:\bt_api_py\bt_api\bt_api_ctp`, HEAD `ce1edd60785eb4c66fefa16a994a66946a1e068f`, with tracked edits and untracked modules/tests | Current working-tree source hashes below; these are not commit-tree hashes. |
| I9 Store/worker | `D:\bt_api_execution_codex_i9_single_worker_20260926`, HEAD `dfeeda7f03878bd4c39ca97462e6e29fa3d754a5`, dirty `contracts.py` and an untracked test | Read only; no execution-store files were changed. |

Current working-file SHA-256 values:

| File | SHA-256 |
|---|---|
| Candidate `src/bt_api_ctp/ctp/client.py` | `5036DF211142D1884EE0B31849E0E42A20E2078966528DA283E74CBA714D0D3A` |
| Candidate `src/bt_api_ctp/query.py` | `46DF8C6FF4BFE4520D01BCC47EF306713443207A3E67021F36321A3DC79E4A9F` |
| Candidate `src/bt_api_ctp/feeds/live_ctp_feed.py` | `901AB9DFBC9797058AA77D4340F4712A2FE961E2D1DD9410475DA12312D30738` |
| Candidate `src/bt_api_ctp/__init__.py` | `60DD839CD1E8CE626F32E22EAA1BC94A5201CD584CC007A7B7A7E0608B9E9888` |
| Candidate `pyproject.toml` | `6295D10501CE71FD3AAFCC2BE2D2B28274BB82D6CA0E94230757D972976669A5` |
| Candidate generated Trader API wrapper `src/bt_api_ctp/ctp/ctp_trader_api.py` | `26A1CAB3D387C5AFB79FC7C041A7F0E333CAFCA708283B05CE65C2B2EFB08610` |
| Candidate fixture bundle `tests/test_iter22_contracts.py` | `D2659D5570E9CFE44D79A8AA16F4E1321D61B1A0894FBEF41C368DB95E606B6A` |
| Candidate callback test `tests/test_ctp_feed.py` | `AF16CA3A612820340D4E9AAB19C619A22EEBC025295609E9A1851C21C8DFCAEB` |
| Candidate stop test `tests/test_ctp_shutdown.py` | `65857C1AF8A25C32266B1AA19D00B55BCD3ABC52D99BDD3CA75062F5EED02BED` |
| Candidate G5 source test `tests/test_ctp_g5_source_unification.py` | `3B754668ED216C9E7A2DD92DB7D3C824FA0E0BA0C745111E4D6548C168E0570A` |
| Candidate copied certificate test `tests/test_ctp_native_query_certificate.py` | `F383B8F06E4EC1A14338A5D135A822C0FF8D8FC0C8583E2CF89ECB82C09EE4BD` |
| Candidate copied login identity test `tests/test_ctp_trader_login_identity.py` | `B2F29B91433BDAF3130D034B66DBDBE734B2F082525C5249E23E6F7DEA3964F9` |
| Candidate position evidence lint fix `src/bt_api_ctp/containers/ctp/ctp_position_evidence.py` | `4580CFCC57BC84891EAF381262455EFA7DCFFB5275FB9DC18EDA9494C5CC3B8B` |
| I13 `src/bt_api_ctp/ctp/client.py` | `B61B32F9A2A2E61035BF88BCB7ACF669C8C7EE8E5BF5D7E1F6E375E69F053EAB` |
| I13 `src/bt_api_ctp/query.py` | `A65BE5FC46A6A7658C1CF017197F1DE14CAD018FB46DBAFD9EAD1CA8FCFC93AD` |
| I13 `src/bt_api_ctp/feeds/live_ctp_feed.py` | `FF27BAE030CBA4E34FC9B1CEE18A7A3AE2210B63016269078BABF2446247C41C` |
| I13 `src/bt_api_ctp/__init__.py` | `0EDD27FD3B7ABAAAF76C865C911EA1A65BCBC32C7AFCB7F53FC91197F26BBD32` |
| I13 `src/bt_api_ctp/containers/ctp/ctp_native_query_certificate.py` (untracked in I13 tree) | `A76E4E1245E84C782FFB9A92B4A804C377FC58C7F3672FD33265E3260E059FBE` |
| I13 `pyproject.toml` | `ACDAD1FDBDDCF8F4C08A709FDB283DF7850E728D9B60D613877067FA93C783F6` |

The candidate manifest version is `2.0.4+iteration41.i9`; the dirty I13
package declares `2.0.3`. The candidate has manual dirty G5 files and is not a
reproducible combined I13 artifact. Do not use either version string as a
wheel pin or native-binary provenance.

## Exact interface comparison

| Interface | Consumer contract / I13 evidence | Candidate state | Compatibility conclusion |
|---|---|---|---|
| Trader construction and selected MD/TD pair | `backtrader_runtime/ctp_trader_client_port.py` factory passes `md_front=` and `ctp_env_profile="config_front_pair"`; it later checks `_bound_md_front` and `ctp_env_profile`. Current I13 `TraderClient.__init__` accepts these keyword-only arguments, validates the pair, stores the bound pair/profile, and exposes the profile. | Candidate constructor ends at `auto_settlement_confirm`; it has no bound MD-front/profile property. | Direct construction from the managed composition raises `TypeError` for the keywords; the binding check also cannot succeed. Must add this as a same-client-generation binding, not a free mutable compatibility attribute. |
| Native Trader login identity observation | Current I13 `ctp/client.py` defines `_TRADER_LOGIN_IDENTITY_SEAL`, `_TraderLoginIdentityObservation`, stamps it in accepted login callback handling, clears it on invalidation, and checks it in `_current_login_identity_locked`. | Candidate now carries the sealed observation under its existing `_query_state_lock` and API/connection generation. Terminal accepted login response identity is required for `is_read_only_ready` and query session scope; mismatch, stale generation, API swap, or disconnect clears it. | Source-only fake tests for the I13 login identity suite pass with the candidate. This is the candidate's existing lock/generation state, not a second lifecycle registry. It does not add the missing bound MD/profile constructor contract. |
| Query evidence certificate | Main `backtrader_runtime/ctp_simulation_query_evidence.py::_sdk_contracts()` imports `_same_scope`, `_scope_for_client`, `_source_for_result` from `bt_api_ctp.containers.ctp.ctp_native_query_certificate`. I13 currently has that untracked module and exports `CtpNativeQueryCertificate`, builder and error from package root. | Candidate contains the same certificate source file and exports its certificate, builder, error, and `CtpOrderActionEvidence`. | Copied I13 certificate/login source tests pass against the candidate without injected bridge fixtures. Parent independently reports the main query verifier target at 34 passed with the candidate and frozen base. These remain fake/source checks, not a combined wheel or native acceptance. |
| Query source shape | I13 `query.py` currently exposes `request_filters` and `explicit_request_filters`; current I13 builders populate these from request values. | Candidate has those consumer fields plus `request_intent_filters`, `request_intent_parameters` and read-back `request_parameters`. Built-in query builders require exact native string getter equality; numeric option-cost floats use a separate typed getter-readback tuple. | Additive shape preserves I9's exact string-key consumer. The candidate now has stronger fake/source evidence for built-in request fields. Native SWIG getter behavior remains unverified, and private `_execute_query` is not a hostile in-process boundary. |
| Bounded stop receipt | I13 client defines `CtpNativeStopReceipt` and `MdClient.stop_and_wait()` / `TraderClient.stop_and_wait()`. Backtrader `ctp_native_shutdown.py` imports the type from `bt_api_ctp.ctp.client` and requires this method/receipt contract. | Candidate exposes `CtpNativeStopReceipt` and `stop_and_wait()` on MD and Trader over its existing one-Join/one-Release-per-exact-API lifecycle tracker. Timeout observes the same Join tracker; it does not issue or terminate Join. Release failure remains poisoned and is visible in the receipt. With no Python observer, `thread_alive=False` means that observer is absent; independent `join_completed` and `native_released` still prevent an active synchronous Join from being accepted. | Focused fake stop/consumer target passed 45 cases locally and in independent review, including actual Backtrader consumer acceptance after completed synchronous Join and rejection while synchronous Join is active. Qualification: the operation calls the existing synchronous `stop()` before applying a bounded Join wait, so it is not a hard wall-clock bound or Windows supervisor. |
| Legacy callback event queues | I13 uses internal `_TraderEventQueueEntry` records with API and connection generations. `wait_order_event`, `wait_trade_event`, and `wait_error_event` unwrap current entries to their original snapshots and discard stale generations. | Candidate preserves raw legacy snapshots and has a separate source-event queue. That source queue currently records only `OnRtnOrder`, `OnRspOrderAction`, and `OnErrRtnOrderAction`; it is a filtered sequence, not a global callback stream. Other financial callbacks and lifecycle callbacks are not all persisted before exposure, and consumers dequeue before any Store/inbox durability step. | Existing public snapshot return shape is preserved, but source completeness, durable-before-publish, stale queue generation suppression and callback-to-Store handoff remain open. The three-event queue cannot be treated as a full source or high-watermark for multi-command ownership. |
| Managed cancel native gate | Main `ctp_trader_client_port.py` calls `submit_order_action(field, request_id, execution_capability=..., runtime_order_id=..., managed_intent_id=..., runtime_action_id=..., managed_cancel_intent_id=...)`, then reads request-bound `CtpOrderActionEvidence`. I13 feed supplies all four values. | Candidate feed/client now expose those names and require complete target (`OrderRef`, `OrderSysID`, `ExchangeID`, `FrontID`, `SessionID`), `ActionFlag="0"`, matching request/action references, and detached native getter equality for opted-in calls. Legacy calls retain prior target alternatives. | This local direct-call seam exists in the candidate. It does not make an I9 prepared queue dispatch, same-store OrderRef reservation or Store receipt connect to the SDK. |
| Managed SimNow credential binding and final authorization | Main `ctp_trader_client_port.py` requires an exact `CtpRuntimeSimNowCredentialBinding`, a callable write-intent verifier, then calls `configure_runtime_simnow_credential_binding`. I13 client binds this to the exact feed owner/front scope and parent `CtpCredentialBindingVerifier`; it mints and consumes a one-native-call `_CtpRuntimeSimNowWriteAuthorization` at the final request gate. | Candidate has the older execution capability gate and managed cancel correlation arguments, but has no binding class, configure method, verifier call, bound MD/profile pair, or one-call managed authorization class/gate. Its constructor rejects the main port's `md_front` and `ctp_env_profile` keywords. The frozen parent source contains `_ctp_credential_binding.py` and `_ctp_execution_authorization.py`, but the current parent verifier consumes `MdIdentityObservation` / `active_md_identity`, neither of which is in this candidate; verifier presence alone cannot close the seam. | Main managed port cannot configure this candidate. Do not add a DTO or constructor fields alone: the required closure includes fresh credential scope verification, exact owner/feed/front matching, config-front-pair generic-arm rejection, write-intent verification, and one-use native gate. This remains a high-coupling review slice. |
| Managed response/receipt handoff | The Backtrader adapter builds `CtpSimulationDispatchReceipt` from raw SDK submit status plus typed action evidence. The I9 single-worker package separately stages `CtpManagedPreparedDispatch`, records queue receipt before publish and dispatches only receipt-backed commands. | Neither I13 SDK source nor candidate declares an SDK `CtpManagedCancelResponse`/queue receipt envelope. Candidate `submit_order_action()` returns the native status, as I13 does; no callback envelope is sent to `SqliteExecutionStore.apply_ctp_verified_dispatch_callback()`. | There is no SDK response-envelope symbol to copy. The parent adapter's receipt and I9 worker's queue receipt are distinct contracts; the verifier-to-Store callback invocation remains absent. SDK-only changes cannot close this cross-package gap. |
| Package exports and version | I13 `__init__.py` exports `CtpOrderActionEvidence` and the three query certificate classes; package metadata/version is `2.0.3`. `CtpNativeStopReceipt` is imported from `ctp.client`, not the package root. | Candidate root exports `CtpOrderActionEvidence` and all three query certificate classes; `CtpNativeStopReceipt` exists in `ctp.client` for the direct shutdown import. Metadata/version is `2.0.4+iteration41.i9`. | Query verifier and direct stop imports are source-compatible for the checked contracts. Candidate version cannot stand in for an accepted I13 combined artifact. |
| Callback source event API / generated SPI coverage | Candidate has `ctp/callback_events.py:CtpTraderCallbackSourceEvent`, exported by that module only. The generated wrapper is the pinned source of callback names. | `ctp_trader_api.py` has 155 `def On*` names (SHA above); corresponding `ctp_wrap.h` has 155 `SwigDirector_CThostFtdcTraderSpi` virtuals. `ctp_wrap.cpp` director dispatch performs Python method lookup (`PyObject_CallMethodObjArgs` for `OnFrontConnected`, near line 6080). `_TraderSpi` currently overrides 23 callbacks; only three are recorded to the source-event queue. | Class-level generated overrides appear supportable from wrapper source, but a fake-base dispatch test has not yet proven the chosen generator mechanism. Until every pinned callback has exact phase/classification/capture behavior and the test enforces inventory equality, the source sequence is incomplete. Current three-event queue is diagnostic only, with `scope_binding="unbound"`; it is not a provider receipt or I9 verifier input. |

## Fake/source verification checkpoint

The current copied five-file source suite passed **302 tests**, with one
layout-only parent normalizer case deselected by prior scope agreement. It ran
with the isolated candidate source, frozen clean base source and main
Backtrader source on `PYTHONPATH` (needed for the stop-consumer integration),
`PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`, and no native/provider calls. Warnings were
the expected absence of a matching Windows `_ctp` binary and the existing
unregistered `ticker` pytest marker. The bundle includes the query readback,
callback source, bounded stop, feed, and Iteration 22 contract tests. This is a
source/fake checkpoint only; it is not a combined SDK build or evidence of
native callback dispatch. Separate installed-package-style copies of the
certificate and login identity tests passed 45 cases and are now also present
in the candidate test tree. They were copied byte-for-byte from the dirty I13
working source listed above; these file hashes describe the copies and are not
clean commit provenance.

The updated Iteration 22 fixtures use accepted fake terminal login callbacks
with exact BrokerID/UserID and trading day instead of directly setting the
sealed-ready state. A login request now consumes its normal request ID; test
expectations were advanced rather than resetting the counter. The eight
previous fixture failures pass in the current bundle. No production identity
validation was relaxed.

## Minimum next integration slices

1. Freeze the stop receipt slice after independent recheck of the updated
   no-observer semantics. Its bounded wait begins after synchronous `stop()`,
   so do not market it as a hard total deadline or supervisor.
2. Define complete callback ingress against the pinned 155-method generated
   Trader SPI inventory before implementing the client hook. The owner must be
   durable before native API creation; PRE_LOGIN connect/auth/login callbacks
   are ordered startup facts; pre-login financial/unknown/error/unexpected
   lifecycle callbacks poison. Bind the exact accepted SDK API/SPI/login
   observation once, then poison on later lifecycle/identity changes. Every
   known callback needs an explicit classification/capture rule; sink failure,
   source gap and overflow poison before event exposure. A fake dispatch test
   must prove generated class-level overrides intercept inherited callbacks.
3. Separately review the complete managed credential-binding/final-gate closure
   before changing the candidate. The parent verifier APIs exist in the frozen
   parent source, but the current verifier requires absent `MdIdentityObservation`
   / `active_md_identity`. Candidate integration must also include source-backed
   accepted login identity, owner/front binding, exact `config_front_pair`
   restrictions, write-intent verification, and the one-use final native grant.
   Add fake tests for zero native calls on stale/mismatched binding and verifier
   rejection. A type/constructor-only shim is not compatible.
4. If stale-queue suppression remains required, add I13's generation data as
   internal queue entries and unwrap to the current legacy snapshot return
   type. Test API replacement while entries are queued and callbacks admitted
   during replacement under the candidate's same generation lock.
5. Keep managed cancel callback-to-Store integration blocked across package
   boundaries. Reconcile the exact `CtpManagedPreparedDispatch`, typed I9
   OrderRef reservation and queue receipt contract with the Backtrader adapter
   before changing SDK return semantics. No SDK response envelope or
   verifier-to-`SqliteExecutionStore.apply_ctp_verified_dispatch_callback()`
   handoff currently exists to copy.

All slices remain source-only and fake-only until the entire source closure,
API tests and generated native artifact provenance are separately accepted.
No combined wheel has been built; no registration/default pin was changed.
