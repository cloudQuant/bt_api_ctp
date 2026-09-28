# G5 I13 / I9+Join1 source-candidate decisions

Status: `FAKE_ONLY_SOURCE_CANDIDATE / NO_WHEEL / NO_DEFAULT_PIN / NATIVE_UNVERIFIED`.

This file records the manual decisions for the 14 `client.py` conflict regions
identified by the preserved I13-vs-I9+Join1 merge audit. The work is confined
to this isolated candidate. It does not register the source, create a wheel,
load a native `_ctp` binary, start a provider session, or change a pin.

## Region decisions

| # | Conflict area | Candidate decision | Evidence / remaining limit |
|---|---|---|---|
| 1 | Imports | Keep I9 lifecycle/callback imports and add only imports needed by typed query filters and order-action evidence. Do not import I13's `MappingProxyType`, `TYPE_CHECKING`, or `urlsplit` paths without their dependent feature closure. | Ruff and fake tests cover imports. Any omitted I13 feature must bring its own reviewed imports. |
| 2 | Module Join/Release state vs I13 stop receipt state | Keep the I9 Join tracker and claim/return state as the sole Join authority. Do not add I13's independent `SESSIONS_RELEASING`/`JOIN_COMPLETED` registries or `CtpNativeStopReceipt`. | Two lifecycle authorities could disagree about the same API. Existing wait reports Join only; no bounded-stop receipt is claimed. |
| 3 | Release helper reservation | Route immediate cleanup and after-Join cleanup through `_release_ctp_native_api_once`; reserve one API ID under the registry lock and call native `Release()` outside that lock. | Fake immediate-release and Join tests exercise the single-flight guard. No native Release was called. |
| 4 | Release result and retention | A normal return records release completion and drops the retained API/SPI. Any exception poisons the API and retains owners; no second Release is attempted. A non-weakref fallback uses `id(api)` and is cleared when the supported factory returns a new API object. | For injected non-weakref wrappers, the fallback assumes registration occurs at the factory boundary. Hand-injected wrappers bypassing that boundary are not an accepted lifecycle path. |
| 5 | Join observer completion | Record the result in the I9 tracker, mark the per-API Join claim returned, then release only through the shared helper. Clear the client's pending fence only after Release succeeds. Keep `wait_native_join`; do not maintain a second completed-API map. | Fake Join tests cover return, failure and pending states. A returned Join alone is not a stop receipt. |
| 6 | MD `stop()` | Keep I9's detach / retain behavior while Join may be active. For immediate cleanup, mark pending before detach and use the shared release helper; an uncertain release keeps restart fenced. | Immediate failure and post-success retry races are covered with fakes. Native timing remains unverified. |
| 7 | Trader `OnRtnOrder` | Validate the exact SPI/API, capture the source event and enqueue the legacy order snapshot under one `_query_state_lock`; invoke `on_order` only after releasing it. | A barrier fake proves API replacement waits until both records commit. The legacy `wait_order_event()` dictionary shape is preserved. |
| 8 | Trader `_api` setter / callback-consumer revocation | Keep the candidate's I9 setter as the generation boundary: revoke callback-consumer lease and write capability, invalidate the old SPI/session, increment API and queue generations, and notify waiters while holding `_query_state_lock`. | Source and action handlers recheck origin while holding this same lock. No setter semantics were imported from a second state map. |
| 9 | Trader start reservation | Keep the pending-Join/release fence in start reservation, in addition to current API and start-generation checks. | Prevents restart after failed immediate Release or a still-live Join. Fake shutdown tests cover both. |
| 10 | Trader Join observer completion | Use the same returned-Join marker, Release guard and pending-fence rule as MD. No separate I13 Join-completed registry. | Fake-only validation; no native CTP runtime acceptance. |
| 11 | Trader Join observer claim | Keep the one-Join-per-API claim and current/retired API identity checks. A second observer cannot create another Join for the same API. | Existing lifecycle tests and the shared state machine cover duplicate claims. |
| 12 | Callback consumer / event queue / push helpers | Keep I9's exclusive token lease and source-only callback queue. Add transactional order/action handlers under its generation lock. Current candidate order/trade/error queues still hold raw snapshots; I13's `_TraderEventQueueEntry` is an internal wrapper that `wait_*_event()` unwraps, so it preserves the public snapshot shape. Its missing behavior is stale API/connection-generation suppression, not a public type change. | Source/order records are committed under one lock. User callbacks run after lock release. The source-only callback queue contains no native API/SPI references. Internal generation-tagged legacy queue entries remain a planned lifecycle integration, not part of this slice. |
| 13 | Query target and order-action evidence | `request_filters` contains only exact string filter fields. Built-in query methods read those named getters from the populated native request before submission and reject missing getters, non-`str` getters, or any mismatch without a query source or native call. Separate typed `request_intent_parameters` / `request_parameters` fields carry option-cost float inputs; getter readback is required and they are not mixed into consumer filter keys. Omitted optional fields remain omitted. This binds the built-in preparation path; it is not a trust boundary against arbitrary same-process calls to private `_execute_query`, which accepts a submission callable independently from the field object. Add typed order-action target history. The feed writes its generated request ID into both native `RequestID` and `OrderActionRef`. Calls with no managed identity keep the legacy cancel target alternatives. Supplying the four managed identity fields opts into a stricter path that requires the complete I9 target tuple and exact request/action reference at the final client gate; the original caller field is read once, a detached native field is built from that snapshot, and all request-binding getters are checked again before send. Partial identities or setter/readback mismatches reject before native send. Submission, current-source validation, source callback event, action evidence update and error snapshot share `_query_state_lock`; successful terminal response means request acceptance only, never exchange cancellation. | Fake tests check string and numeric getter round trips, setter truncation/ignore/normalization/missing getter/wrong-type rejection with zero native calls, exact consumer filter keys, legacy target behavior, managed full-tuple acceptance, changing caller getter containment, detached setter/readback mismatch rejection and zero native calls, action-ID non-reuse after native exception, and a callback/API-swap barrier. No provider callback evidence is asserted. |
| 14 | Trader `stop()` / stop receipt | Keep the common immediate/deferred Release state machine and pending fence; omit I13 `stop_and_wait()` / `CtpNativeStopReceipt` until that separate API contract is independently reviewed against the selected lifecycle. | `wait_native_join()` describes Join only. This candidate makes no stronger shutdown-complete claim. |

## Explicitly unresolved integration

The client source has local choices for all 14 regions above, but this is not a
unified I13 artifact. The I13 package version/export conflicts in `pyproject.toml`
and `src/bt_api_ctp/__init__.py` remain unresolved. I13 dirty-source changes
outside query target metadata and order-action callback evidence were not
copied. The order/trade/error queue wrappers and bounded stop receipt remain
excluded. A separate review must compare the exact final source against I13's
current dirty snapshot and review the remaining API/consumer callsites before
any wheel or pin decision.

The candidate's `TraderClient.submit_order_action` now accepts I13's four
managed-cancel identity arguments (`runtime_order_id`, `managed_intent_id`,
`runtime_action_id`, and `managed_cancel_intent_id`). Supplying any of them
requires all four, validates their formats and action-ID relationships, and
selects the strict native target check while holding `_query_state_lock`
before `ReqOrderAction`. The client reads the caller field once, builds a
detached native field from that immutable snapshot, and compares the detached
field getters with the snapshot before dispatch; the native call receives only
the detached object. The feed carries those values to the client and validates
its complete target before allocating a request ID. These values are
correlation metadata; they do not grant write authority or replace the existing
`execution_capability` gate. Calls that omit all four preserve the legacy
target alternatives. The I13 feed response envelope was not added. The separate
Store reservation / receipt-before-send handoff and typed SDK OrderRef request
contract were not changed; normal Store construction remains closed for
managed requests.

## Independent QA blockers and evidence level

For built-in query methods, string `request_filters` now records native getter
readback of each intended string field before submission; every value must
match the separate string intent exactly. A mismatch, missing getter, or wrong
getter type rejects before native send and returns no query source. Option
trade-cost `InputPrice` and `UnderlyingPrice` remain typed finite floats in
separate intent/readback tuples, with the same pre-send getter equality rule;
they are not coerced to strings or added to the main consumer's exact filter
key sets. Fake setter truncation, ignore, normalization, missing getter, and
wrong-type cases reject with zero native calls. This is fake/source evidence
for the built-in preparation path, not native SWIG acceptance proof. Also, the
private `_execute_query` helper still accepts `submit` separately from the
request field; an arbitrary same-process caller could pair a matching readback
field with a callable that submits a different field. Built-in query closures
use the same local field, but the helper is not a hostile-in-process boundary
or general callable-to-field attestation.

Legacy calls still accept either the local tuple `OrderRef + FrontID +
SessionID` or the exchange tuple `OrderSysID + ExchangeID`, preserving their
public behavior. The explicit managed path now rejects those partial shapes
and requires `OrderRef + OrderSysID + ExchangeID + FrontID + SessionID`,
`ActionFlag == "0"`, `RequestID == request_id`, and `OrderActionRef ==
request_id` before native dispatch. The detached native getters must read back
exactly that same tuple, so a caller-field mutation or native setter
normalization rejects before send. Fake regressions show the original mutable
field is read once, the detached native argument matches the recorded target,
and a setter-normalized `OrderSysID` is rejected before native dispatch. This
closes the candidate's local target validation gap for callers using that
opt-in. It does not prove that an I9
prepared dispatch, same-store OrderRef reservation, SDK call, and callback
mapper share one end-to-end object; the Store/SDK handoff remains unconnected.
This audit does not recommend broadening or weakening native query filters to
bridge that remaining gap.

Finally, neither side currently supplies the complete callback dispatch
handoff: there is no envelope/verifier-to-
`SqliteExecutionStore.apply_ctp_verified_dispatch_callback()` invocation in
this candidate or the I9 mapper/runtime. The candidate's typed action history
is local SDK evidence only; it is not an applied Store receipt. Managed
callback-to-Store integration remains blocked until that handoff and its
verification contract exist and are tested.

The I13 reference query set also has numeric request parameters. The candidate
preserves option-cost inputs only as typed float intent/readback evidence; main
I9 query verification currently promotes only its exact string filter sets. No
numeric parameter is claimed as an I9-supported execution query scope.

Read-only comparison against dirty I13 submodule commit
`ce1edd60785eb4c66fefa16a994a66946a1e068f` under dirty parent
`d3674e19a11b9f35f19ae756899bcf18854c8c46` found that its `query.py` exposes
`request_filters` and `explicit_request_filters`, but not this candidate's
intent/readback split or numeric parameter tuples. Its feed writes both cancel
request fields, and its client carries the managed-cancel arguments plus
`CtpNativeStopReceipt` API. The isolated candidate accepts the managed-cancel
argument names but does not include the I13 receipt or managed-authorization
implementation. The I13 checkout also has tracked and untracked changes and
was not edited or treated as a clean merge base.

The lifecycle's ID fallback is valid for APIs created by the two supported
factory callsites, which call `_register_ctp_native_api` immediately after
creation. It does not promise safe identity reuse for manually injected,
non-weakref API wrappers that bypass those factories.

## Verification

All validation was fake/offline. The standalone candidate's `tests/conftest.py`
indexes a deeper checkout layout than this isolated worktree provides, so
tests were copied to `D:\temp\bt_api_ctp_g5_unify_fake_pytests_20260926` and
run from `D:\source_code\backtrader` with `PYTHONPATH` set to this candidate's
`src` directory. The exact focused command was:

```powershell
$testRoot='D:\temp\bt_api_ctp_g5_unify_fake_pytests_20260926'
$srcRoot='D:\bt_api_ctp_i13_g5_manual_candidate_20260926\tests'
$names=@('test_ctp_g5_source_unification.py','test_ctp_native_callback_events.py','test_ctp_shutdown.py','test_ctp_feed.py','test_iter22_contracts.py')
New-Item -ItemType Directory -Force -Path $testRoot | Out-Null
foreach($name in $names){ Copy-Item -LiteralPath (Join-Path $srcRoot $name) -Destination $testRoot -Force }
$env:PYTHONPATH='D:\bt_api_ctp_i13_g5_manual_candidate_20260926\src'
python -m pytest ($names | ForEach-Object { Join-Path $testRoot $_ }) -q --tb=short -k 'not gateway_quote_v2_serialized_payload_reaches_parent_normalizer'
```

Result: **264 passed, 1 deselected** in 54.83 seconds. The deselected test
resolves a parent package through `Path(__file__).parents[3]`, which the
required temporary test-copy path does not provide. Pytest reported the
expected warning that no matching native extension was loaded and one
unregistered `ticker` marker warning from the copied test file. Ruff passed for
all modified Python files, `compileall` passed, and `git diff --check` passed.

The first managed-cancel compatibility run exposed that I9's feed did not put
its generated request ID into the native `RequestID` / `OrderActionRef` fields
required for target-bound evidence. Those two field assignments were added to
the isolated feed source; both previously failing managed-cancel tests then
passed and are included in the 264-pass run.

After adding the explicit managed-cancel target contract, the same fake-only
test set was copied to
`D:\temp\bt_api_ctp_g5_managed_cancel_fake_pytests_20260926` and run from
`D:\source_code\backtrader` with `PYTHONPATH` set to the candidate `src`:

```powershell
$testRoot='D:\temp\bt_api_ctp_g5_managed_cancel_fake_pytests_20260926'
$srcRoot='D:\bt_api_ctp_i13_g5_manual_candidate_20260926\tests'
$names=@('test_ctp_g5_source_unification.py','test_ctp_native_callback_events.py','test_ctp_shutdown.py','test_ctp_feed.py','test_iter22_contracts.py')
New-Item -ItemType Directory -Force -Path $testRoot | Out-Null
foreach($name in $names){ Copy-Item -LiteralPath (Join-Path $srcRoot $name) -Destination $testRoot -Force }
$env:PYTHONPATH='D:\bt_api_ctp_i13_g5_manual_candidate_20260926\src'
python -m pytest ($names | ForEach-Object { Join-Path $testRoot $_ }) -q --tb=short -k 'not gateway_quote_v2_serialized_payload_reaches_parent_normalizer'
```

Result at that checkpoint: **275 passed, 1 deselected** in 52.59 seconds. Warnings state
that no matching native extension was loaded and the copied test file uses an
unregistered `ticker` marker. `compileall`, Ruff, and `git diff --check` passed
for the candidate changes. No wheel was built and no pin or registration was
changed.

After adding the detached-field getter round-trip and setter-normalization
regression, the same five-file fake-only command was rerun: **277 passed, 1
deselected** in 55.20 seconds. The same two warnings remain (no matching native
extension loaded; unregistered `ticker` marker). No native call path was used.

After adding typed numeric query-parameter getter readback, the five-file
fake-only bundle was copied to
`D:\temp\bt_api_ctp_g5_query_param_bundle_20260926` and rerun with the same
candidate `src` on `PYTHONPATH`: **292 passed, 1 deselected** in 58.31 seconds.
The deselected case is only the copied-file `Path(__file__).parents[3]` layout
assumption; warnings were missing native extension and the copied-file
unregistered `ticker` marker. The isolated query source test passed separately:
**34 passed** in 3.14 seconds with the expected no-matching-native-extension
warning. Candidate `compileall`, Ruff and `git diff --check` passed. Parent
independently reran the 34-test target with the frozen clean base source on
`PYTHONPATH`: **34 passed** in 3.89 seconds with the same native-extension
warning. These are source/fake results, not a combined artifact or native
acceptance.
