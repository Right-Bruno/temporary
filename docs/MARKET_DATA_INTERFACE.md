# Provider-Independent Market Data Interface

Status: **Phase 1, Stage 1 implemented**. This layer is provider-independent and
has no TradingView, MCP, MT5, network, or SQLite dependency. It does not
retrieve live data or write candles.

## 1. Purpose and scope

The interface gives the rest of the system one way to request historical or
live candles without coupling strategy code to a specific provider. Stage 1
provides:

- common request/series contract types;
- evidence-only candle completion (D-05);
- structural and market-data validation;
- a CSV adapter built on the existing validator;
- a canonical instrument registry with explicit source bindings.

Out of scope for Stage 1:

- live TradingView access, MCP, CDP, or chart manipulation;
- MT5 broker-symbol mapping;
- SQLite writes, schema migration, ingestion, and raw-payload retention;
- strategy rules, session calendars, gap classification, or interpolation.

## 2. Identity model

Three distinct identities are preserved:

| Identity | Example | Owner |
|---|---|---|
| Canonical instrument | `EURUSD` | `InstrumentRef.canonical_id` |
| Provider-native symbol | `FOREXCOM:EURUSD` | `SourceBinding.source_symbol` and `Candle.symbol` |
| Candle provenance label | `tradingview-mcp:FOREXCOM` | `SourceBinding.source_label` and `Candle.source` |

The stored candle identity remains exactly:

```text
(open_time_utc_us, candle.symbol, candle.timeframe, candle.source)
```

The canonical instrument id belongs to the request and the `CandleSeries`; it
never overwrites `Candle.symbol` or `Candle.source`. Adapter names, canonical
ids, provider flags, and source labels are separate values and are never
interchanged.

`SourceBinding.status` is `VERIFIED`, `CANDIDATE`, or `TO_VERIFY`. Only an
explicit owner-verified binding may be `VERIFIED`. The shipped configuration
contains no verified binding because D-07 (manual ticker verification) remains
deferred.

## 3. Contract types

Defined in `src/trading_automation/data/market_data/contract.py`:

- `InstrumentRef(canonical_id, asset_class, display_name)`
- `SourceBinding(adapter, source_symbol, source_label, status)`
- `CandleRequest(instrument, timeframe, count, source_filter)`
- `CandleSeries(instrument, timeframe, candles, fetched_at_utc, metadata,
  raw_payload_sha256, raw_payload_bytes)`
- `AdapterMetadata(adapter_name, adapter_version, completion_mode, sources,
  details)`
- `MarketDataCapabilities(max_bars, supports_date_range, provides_volume,
  completion_modes)`
- `SourceHealth(adapter_name, available, checked_at_utc, reason)`
- `MarketDataSource` protocol: `name`, `capabilities()`, `health()`,
  `get_candles(request)`

`CandleRequest.count` is between 1 and 500. The current supported timeframe
labels are `M5`, `H1`, and `D1`.

A `CandleSeries` requires exactly one source-native symbol, exactly one source
label, a matching timeframe, strictly ascending open times, and no duplicate
open times. Provider-specific diagnostics such as a chart symbol belong in
`AdapterMetadata.details`; they are not required by strategy code.

`raw_payload_sha256` and `raw_payload_bytes` are optional provenance metadata.
Raw payload bytes are not retained or written anywhere.

## 4. Completion policy (D-05)

The default is `CompletionMode.EVIDENCE`:

1. A candle is established as completed when a later candle in the same
   validated series exists.
2. Otherwise it remains incomplete.
3. The final candle keeps its declared state. It is never upgraded merely
   because time has passed.
4. A completed candle is never downgraded.
5. There is no seven-day D1 rule, no elapsed-time completion, and no inferred
   session calendar.

`CompletionMode.DECLARED` preserves the source's `is_closed` field exactly.
The CSV adapter uses declared completion because the existing CSV contract
makes `is_closed` authoritative.

`CompletionMode.ELAPSED` is defined for future work but raises
`UnsupportedRequestError` in Phase 1.

## 5. Series validation

`validate_candles(...)` checks a series against the request's canonical
instrument and source binding. `validate_series(...)` re-checks a constructed
`CandleSeries`. Checks include:

- non-empty series and required values;
- supported timeframe and matching canonical/source association;
- timezone-aware timestamps;
- strictly ascending, unique candle identities;
- `high >= max(open, close)`, `low <= min(open, close)`, `high >= low`;
- finite, positive prices;
- no candle opening after the validation instant;
- no interpolation, missing-candle creation, or conflicting-candle resolution.

Gaps are not classified here; missing data remains unclassified until an
owner-approved instrument calendar exists.

## 6. CSV adapter

`src/trading_automation/data/market_data/csv_source.py` reuses the existing
`validate_csv()` implementation without modifying it.

```python
from pathlib import Path
from trading_automation.data.market_data import (
    CandleRequest, CsvMarketDataSource, InstrumentRef, SourceBinding, Timeframe,
)

instrument = InstrumentRef("EURUSD", "forex", "Euro / US Dollar")
binding = SourceBinding(
    adapter="csv",
    source_symbol="EURUSD",
    source_label="TEST_FIXTURE",
    status="CANDIDATE",
)
source = CsvMarketDataSource(
    Path("data/sample_candles.csv"),
    instrument=instrument,
    binding=binding,
)
series = source.get_candles(
    CandleRequest(instrument=instrument, timeframe=Timeframe.H1, count=2)
)
```

Behaviour:

- Every candle keeps its declared `source` exactly; `TEST_FIXTURE` and other
  valid labels are never relabelled.
- `is_closed` is preserved exactly (declared completion).
- A single series with a different source label than the binding is rejected.
- Multiple sources for the same symbol/timeframe are rejected with
  `AmbiguousSourceError` unless `source_filter` is supplied.
- The adapter returns the most recent `count` matching candles. If fewer exist,
  all available candles are returned.
- `health()` reports file readability; a successful catalog/validation read is
  separate from provider authenticity.
- Metadata records the filename (basename only), SHA-256, source timezone, and
  validation status. No full local path and no raw payload are stored.
- No SQLite connection or write is made.

## 7. Symbol registry and configuration

`src/trading_automation/data/market_data/symbols.py` loads
`config/instruments.toml` with `SymbolRegistry.load_default(root)`.

```toml
schema_version = 1

[[instruments]]
canonical_id = "EURUSD"
asset_class = "forex"
display_name = "Euro / US Dollar"

[[instruments.bindings]]
adapter = "tradingview"
source_symbol = "FOREXCOM:EURUSD"
source_label = "tradingview-mcp:FOREXCOM"
status = "CANDIDATE"
```

Rules enforced at load time:

- `schema_version` must be `1`; unknown keys are rejected;
- canonical ids use uppercase letters, digits, `_`, `.`, or `-`;
- asset classes are `forex`, `metals`, or `indices`;
- canonical ids and full source-binding identities are unique;
- MT5/MetaTrader adapters are rejected in Phase 1;
- `VERIFIED` requires both `verified_at` (timezone-aware ISO-8601) and
  `verification_reference`;
- `CANDIDATE` and `TO_VERIFY` bindings must not carry verification evidence.

The shipped registry contains canonical instruments named by the approved STS
universe. The only provider bindings are CANDIDATE `FOREXCOM` mappings for
`EURUSD` and `GBPUSD`. No index or metal ticker is guessed or marked verified.

## 8. Errors

All market-data failures derive from `MarketDataError`:

| Error | Meaning |
|---|---|
| `SourceUnavailableError` | The configured source cannot currently provide data. |
| `InstrumentNotMappedError` | No matching canonical/provider binding exists. |
| `AmbiguousSourceError` | More than one source or binding matches without an explicit filter. |
| `UnverifiedBindingError` | A binding exists but is not verified for the requested use. |
| `UnsupportedRequestError` | The request is outside the adapter's capabilities. |
| `SourcePreconditionError` | The source is reachable but its state does not match the request. |
| `MarketDataValidationError` | A request, series, or payload violated the data contract. |

`SeriesValidationError` extends `MarketDataValidationError` and exposes
`issues` with stable codes such as `FUTURE_CANDLE`, `OHLC_INCONSISTENT`, and
`DUPLICATE_CANDLE_IDENTITY`.

## 9. Boundaries and remaining work

- No live data, MCP, CDP, chart access, or MT5 integration exists in Stage 1.
- No SQLite candle writes or schema changes are made.
- Stage 2 will add the MCP stdio client and catalog-only verification; it will
  not retrieve candles.
- D-06 (live session verification), D-07 (manual ticker verification), D-09
  (raw payload retention), and D-10 (history beyond 500 bars) remain deferred.
- The completed-candle revision policy and instrument-specific session
  calendars remain unresolved and are not invented here.

## 10. Tests

Stage 1 tests:

```text
tests/test_market_data_contract.py
tests/test_market_data_completion.py
tests/test_market_data_validate.py
tests/test_csv_market_data_adapter.py
tests/test_symbol_registry.py
```

Run the complete suite with:

```powershell
python -m unittest discover -s tests -v
```
