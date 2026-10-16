# SCN02/013 CTP query scope boundary fix

Repository: `bt_api/bt_api_ctp`, branch `dev`, base `da4f7badc36ca4a8b7b1020048fb0b3598021f32`.

`CtpInstrumentMetadataReader.read()` captured a fresh Trader scope after the instrument query and then used that later capture time as the lower bound for the query's start time. The query therefore failed its own valid start-time check. The reader now validates the query against its pre-query scope after confirming the post-query scope still identifies the same Trader session. The returned metadata continues to use the verified current scope, and the existing quote-session identity check remains unchanged.

Focused regression coverage models a delayed post-query scope capture and confirms the exact query start falls between the initial and later captures. A second test changes the post-query connection generation and confirms the reader rejects the result as `TRADER_SESSION_CHANGED`.

Verification on the final candidate bytes:

```text
C:/anaconda3/python.exe -m pytest -p no:asyncio -q tests/test_instrument_metadata.py
5 passed, 1 warning in 12.97s
```

The warning is the existing unknown `asyncio_default_fixture_loop_scope` pytest configuration option. Ruff check passed for both changed Python files. Tests use the package's real `TraderClient`, query result/certificate types, and `CtpMarketStream` with synthetic native callback leaves; no account, credentials, or external CTP provider was used. This is focused source evidence, not account/provider acceptance.
