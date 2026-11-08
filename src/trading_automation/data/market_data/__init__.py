"""Provider-independent market-data interface.

Stage 1 provides the common contract, evidence/declared completion policy,
series validation, a CSV adapter, and a canonical instrument registry.  It has
no TradingView, MCP, MT5, network, or SQLite dependency.
"""

from trading_automation.data.market_data.completion import (
    apply_completion,
    apply_declared_completion,
    apply_evidence_completion,
)
from trading_automation.data.market_data.contract import (
    MAX_BARS_PER_REQUEST,
    AdapterMetadata,
    AmbiguousSourceError,
    BindingStatus,
    CandleRequest,
    CandleSeries,
    CompletionMode,
    InstrumentNotMappedError,
    InstrumentRef,
    MarketDataCapabilities,
    MarketDataError,
    MarketDataSource,
    MarketDataValidationError,
    SourceBinding,
    SourceHealth,
    SourcePreconditionError,
    SourceUnavailableError,
    Timeframe,
    UnsupportedRequestError,
    UnverifiedBindingError,
)
from trading_automation.data.market_data.csv_source import (
    CsvMarketDataSource,
    supported_timeframes,
)
from trading_automation.data.market_data.symbols import (
    ALLOWED_ASSET_CLASSES,
    InstrumentDefinition,
    SymbolRegistry,
)
from trading_automation.data.market_data.validate import (
    SeriesValidationError,
    SeriesValidationIssue,
    validate_candles,
    validate_series,
)

__all__ = [
    "MAX_BARS_PER_REQUEST",
    "ALLOWED_ASSET_CLASSES",
    "AdapterMetadata",
    "AmbiguousSourceError",
    "BindingStatus",
    "CandleRequest",
    "CandleSeries",
    "CompletionMode",
    "CsvMarketDataSource",
    "InstrumentDefinition",
    "InstrumentNotMappedError",
    "InstrumentRef",
    "MarketDataCapabilities",
    "MarketDataError",
    "MarketDataSource",
    "MarketDataValidationError",
    "SeriesValidationError",
    "SeriesValidationIssue",
    "SourceBinding",
    "SourceHealth",
    "SourcePreconditionError",
    "SourceUnavailableError",
    "SymbolRegistry",
    "Timeframe",
    "UnsupportedRequestError",
    "UnverifiedBindingError",
    "apply_completion",
    "apply_declared_completion",
    "apply_evidence_completion",
    "supported_timeframes",
    "validate_candles",
    "validate_series",
]
