# Iteration10 CTP pre-start custody repair

Repository: `bt_api_ctp`, existing `dev`; acceptance parent
`68a5e1147cb98ad341b0cae79bc39a24f6a74dd0`. Scope: this record,
`src/bt_api_ctp/account_provider.py`, and `tests/test_account_provider.py`.

A constructor failure in the exact loaded `CtpRequestDataFuture` occurs before
it retains a TraderClient and before native startup. The provider now reports
that proven state as native-free at the connect-exception boundary. The check
uses the already loaded exact class and does not import the feed on failure.
Custom feeds remain uncertain. Closing an unknown owner without a retained
trader now returns false and preserves custody, instead of downgrading unknown
to native-free. Existing native owners still require their complete stop receipt.
Gateway's unknown-owner rejection and its regression test are unchanged.

Independent static review accepted this narrow source change. Root ran the final
bytes once with `C:/Anaconda3/python.exe`: **7 passed in 2.24 seconds**, exit 0,
no warnings, empty stderr, 183 source/config pins and three repository HEADs
unchanged. Output: `D:/temp/ctp-pre-native-owner-root-r1-20261017/`;
spec: `D:/bt_api_py/.codex-tmp/ctp-pre-native-owner-root-r1-spec-20261017.json`.
Stdout SHA-256:
`de5e4f54909858d47f2eb339f6d58099634c7c9ffba63805cfcc7be5c6ab9238`.

Accepted source SHA-256:
- provider: `23d6b68b01b52827995d6da162727f0663649ef0d2768bb07913ca6d28a1fe02`
- test: `48af591049478f6f44b6181a2596705550125e7bda15541024bc60330ed18540`

The new regression runs the real feed/provider with a failing TraderClient
constructor leaf and verifies that start is never called. It also verifies
unknown custom-feed custody; the existing interrupted-native receipt regression
passes unchanged. Ordinary package imports are allowed and native-module absence
is not claimed. No native API creation/start, account call, or retained-session
operation is accepted by this source-component round. VENUE01/02 remain pending.
The author's earlier 7-pass run preceded the final lookup refinement and is not
substituted for this Root result. Accepted QA is not repeated for delivery.

The commit containing this record identifies the accepted child revision; the
parent SDK records its exact gitlink in the subsequent scoped delivery.
