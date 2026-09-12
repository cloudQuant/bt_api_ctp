# Read-only CTP instrument discovery

`discover_instruments.py` exports the complete instrument response visible to an
authenticated SimNow counter. It queries futures, options and other returned
product classes without selecting a trading strategy or inferring exercise style.
The example uses the packaged public `TraderClient` and
`normalize_ctp_instrument`; it creates no orders, cancellations or settlement
confirmations. It does not query a fee for every instrument.

Run from the repository root using the Anaconda environment. `PYTHONPATH` below
explicitly chooses the current source package; omit it when validating an
installed package. The manifest records the actual imported package path and
native hashes.

```bash
PYTHONPATH="$PWD/bt_api/bt_api_ctp/src" \
  /Users/yunjinqi/opt/anaconda3/bin/conda run --no-capture-output -n base python \
  bt_api/bt_api_ctp/examples/discover_instruments.py \
  --env-file /absolute/path/to/local.env \
  --profile set1_group1 \
  --output-dir /absolute/path/to/new-discovery-directory
```

The supported frozen profiles are `set1_group1`, `set1_group1_vpn` and
`set1_group2`. The selector checks the named TD/MD pair for reachability; it never
accepts arbitrary fronts, switches to production or substitutes another profile.
Only a trader session is opened. TCP reachability does not prove authentication,
current market status or trading eligibility.

For counters whose unfiltered query does not finish within the timeout, select
explicit query scopes with repeatable filters, for example:

```bash
PYTHONPATH="$PWD/bt_api/bt_api_ctp/src" \
  /Users/yunjinqi/opt/anaconda3/bin/conda run --no-capture-output -n base python \
  bt_api/bt_api_ctp/examples/discover_instruments.py \
  --env-file /absolute/path/to/local.env --profile set1_group1_vpn \
  --instrument-filter DCE:m2701 --instrument-filter CZCE:SA701 \
  --include-depth --query-timeout 20 \
  --output-dir /absolute/path/to/new-filtered-directory
```

Filter semantics belong to the counter. An instrument filter may prefix-match a
future and all its related option contracts; it must not be interpreted as an
exact-instrument guarantee. Every requested filter gets its own instrument query
and, when that query is complete, its own optional depth query. Any unfinished
requested query makes the union partial. Rows are combined by native
`(ExchangeID, InstrumentID)`, with the latest returned row winning in request
order. Each filter's terminal/identity/query evidence remains under distinct keys
such as `queries["instruments:0"]` and `queries["depth_market_data:0"]`;
`selected_coverage` records the actual filters and per-query row counts. No
aggregate `QueryResult` is fabricated. The unfiltered mode retains the original
`queries["instruments"]` key.

A successful filtered run is `COMPLETE_FILTERED_VISIBLE_UNIVERSE` with
`scope=filtered_visible_universe`; it certifies only those selected counter
responses, never full-market coverage. Omitting all filters preserves the default
empty-filter complete-visible-universe request.

Credentials are read from the process environment and, if explicitly supplied,
the single `--env-file`. No other `.env` is searched. Supported canonical names
are `CTP_BROKER_ID`, `CTP_USER_ID`, `CTP_PASSWORD`, `CTP_APP_ID` and `CTP_AUTH_CODE`;
the corresponding `SIMNOW_*` names and existing lowercase `simnow_*` aliases are
also recognized. Process values take precedence even across aliases. Broker,
user and password are required; app/auth defaults remain the public client's
defaults. The env parser accepts simple optionally quoted `KEY=value` lines and
`export KEY=value`; it does not expand shell expressions or environment variables.
Never commit the env file or include its contents in an output artifact.

`--include-depth` adds one public depth-snapshot query per selected scope. This is a
point-in-time counter response, not a live subscription or proof of executable
three-leg prices. It may be unsupported by a counter; if requested but incomplete,
the whole run remains partial. Defaults are `--query-timeout 120`,
`--login-timeout 30`, and `--probe-timeout 3`, all in seconds. Query throttling is
owned by `TraderClient`.

The output directory must not already exist. Artifacts are:

| File | Content |
| --- | --- |
| `discovery.json` | Session fingerprint/generation/TradingDay, query terminal evidence, zero-write counters, source/package/native provenance, counts and artifact hashes |
| `progress.json` | Current initialization/login/query/stop phase, UTC timestamp, returned record count and provisional write counters; no account or credential fields |
| `raw_instruments.json` | Returned native instrument fields with accidental account fields excluded; nonfinite numbers become null and byte strings are decoded |
| `instruments.json` | Normalized records under `records`; unique contract identity uses `exchange_id` plus `instrument_id` |
| `instruments.csv` | The same normalized fields in UTF-8 CSV |
| `depth_market_data.json` | Optional returned native depth fields |

Completion requires successful terminal packets, the same account fingerprint
and connection generation as the stable read-only session, unchanged TradingDay,
and zero `settlement_confirm`, `order_insert` and `order_action` counts before and
after the session. Successful terminal zero-row responses remain distinguishable
from missing responses. Empty filters mean the complete **counter-visible** set,
not a claim to enumerate every contract listed by every exchange.

Exit code `0` and `COMPLETE_VISIBLE_UNIVERSE` (or the explicitly filtered status)
certify only the requested read-only
query/export. `PARTIAL` or `BLOCKED` returns `2`; available rows are retained with
`complete: false`. Native exceptions and callback stdout/stderr are suppressed;
reports retain stable reason codes rather than raw exception text. An explicitly
unsupported login ABI fails before probing. Older diagnostics without that
optional field still use the public client's existing guarded login, preserving
the stable `ctp_trader_login_abi_unverified` reason when it refuses a binding.
The client is stopped in `finally`, including query and export failures; stop
failure prevents a successful result. Never reload a rebuilt native module into
an old Python process.

An initial `PARTIAL` manifest is written before connecting, and JSON updates use
atomic replacement. The final `discovery.json` is the authority for export
completion: interrupted CSV/JSON export leaves the initial partial manifest in
place. During a long query, inspect `progress.json`; returned-record counts become
available when a public query call returns, not for each native callback.

`exercise_style`, settlement rules, usable margin, account fees, actual remaining
trading days and the affordability of a strategy require further evidence.
In particular, unknown exercise style is retained as null, not set to European,
and American options are not silently excluded. This tool does not approve
orders or prove arbitrage/profitability.

Offline tests (no credentials or network required):

```bash
/Users/yunjinqi/opt/anaconda3/bin/conda run --no-capture-output -n base python -m pytest \
  bt_api/bt_api_ctp/tests/test_instrument_discovery_example.py -q
```

## Account cost screening

After a discovery process has finished, a separate read-only session can collect
costs for selected futures and same-strike, same-expiry call/put pairs:

```bash
PYTHONPATH="$PWD/bt_api/bt_api_ctp/src" \
  /Users/yunjinqi/opt/anaconda3/bin/conda run --no-capture-output -n base python \
  bt_api/bt_api_ctp/examples/query_option_pair_costs.py \
  --discovery-dir /absolute/path/to/completed-discovery-directory \
  --env-file /absolute/path/to/local.env --profile set1_group1_vpn \
  --max-products 15 --query-timeout 30 \
  --output-dir /absolute/path/to/new-cost-directory
```

This accepts complete filtered discovery too. It checks artifact hashes and the
same profile, account and TradingDay; each cost query must belong to the new
session's stable generation. Source quotes remain historical reference prices.
The collector chooses one eligible underlying per product by observed futures
volume and a nearby strike with matching calls/puts; this is a bounded research
sample, not a global liquidity optimum. Contract reference margin is only a rough
prefilter; calculations require account-specific absolute rates (`IsRelative=0`).
An explicitly empty native cost `ExchangeID` is attributed to the submitted
request scope with a recorded source label. Missing or contradictory identity
fields remain unusable; native records are not rewritten.

`costs.json` retains requested prices, raw public fields and terminal evidence.
`capital_results.json` and `.csv` estimate one future plus one/two **long** options
for a CNY10,000 budget and separate CNY0/CNY2,000 reserve scenarios. Puts pair with
a long future; calls with a short future. Quantities are not assumed delta-neutral.
`screen_option_pairs.estimate_buy_option_pair` performs the offline arithmetic.

Each capital row requires its own future margin, future fee and option fee proof;
a missing seller trade-cost response does not invalidate a long-option estimate.
An incomplete unrelated query can leave the collection partial while complete
rows remain explicitly usable for static analysis. A changed session or invalid
zero-write proof invalidates all rows. Insufficient displayed quantity yields no
estimate for that size. All rows retain `economic_signal=NOT_EVALUATED`,
`risk_admission=NOT_EVALUATED` and `quote_execution=NOT_VERIFIED`.

No existing account position/available-balance assessment, financing, exercise
processing, dynamic delta hedge or execution/PnL simulation is performed. The
reserve is a scenario input, not a proved stress-loss bound or a user-mandated
allocation rule. American options are retained for research; the collector does
not apply European put-call parity as a profitability gate.
