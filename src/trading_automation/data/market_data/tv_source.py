"""TradingView market-data adapter with fake-transport support.

Stage 3 connects the existing MCP client to the provider-independent Market
Data Interface.  All development and verification use an injected fake
transport; no real TradingView or CDP connection is ever made.

The adapter reuses Stage 1 contracts (CandleRequest, CandleSeries,
AdapterMetadata, InstrumentRef, SourceBinding), validation
(validate_candles), completion (apply_evidence_completion), and the Stage 2
McpStdioClient abstraction.  It implements the existing MarketDataSource
Protocol.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Mapping

from trading_automation.data.market_data.completion import apply_evidence_completion
from trading_automation.data.market_data.contract import (
    AdapterMetadata,
    CandleRequest,
    CandleSeries,
    CompletionMode,
    InstrumentNotMappedError,
    InstrumentRef,
    MarketDataCapabilities,
    MarketDataValidationError,
    SourceBinding,
    SourcePreconditionError,
    SourceUnavailableError,
    Timeframe,
    UnsupportedRequestError,
)
from trading_automation.data.market_data.validate import validate_candles
from trading_automation.domain.candles import Candle, as_decimal
from trading_automation.domain.timezones import to_epoch_microseconds, to_new_york

_ADAPTER_NAME = "tradingview"
_ADAPTER_VERSION = "1"
_MAX_BARS = 500
_DEFAULT_COUNT = 100

# Approved TradingView resolution -> project timeframe mapping (owner approved).
# TradingView returns native resolution strings such as "5", "60", "D".
# Production verification of any additional resolution strings remains open
# (D-07 deferred); no speculative aliases are added.
RESOLUTION_TO_TIMEFRAME: dict[str, Timeframe] = {
    "5": Timeframe.M5,
    "60": Timeframe.H1,
    "D": Timeframe.D1,
}

# Plausible Unix-timestamp ranges used to deterministically interpret the bar
# "time" field.  These ranges do not overlap, so a value unambiguously maps to
# one unit.  Production timestamp-unit verification remains outstanding.
_EPOCH_SECONDS_MIN = 946_684_800  # 2000-01-01 00:00:00 UTC in seconds
_EPOCH_SECONDS_MAX = 410_244_4800  # 2100-01-01 00:00:00 UTC in seconds
_EPOCH_MILLIS_MIN = _EPOCH_SECONDS_MIN * 1_000
_EPOCH_MILLIS_MAX = _EPOCH_SECONDS_MAX * 1_000


def _resolve_now(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    if not isinstance(now, datetime):
        raise TypeError("now must be a datetime")
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware; naive timestamps are rejected")
    return now.astimezone(timezone.utc)


def _map_resolution(resolution: Any) -> Timeframe:
    if not isinstance(resolution, str) or not resolution:
        raise SourcePreconditionError(
            "chart state resolution is missing or not a string",
        )
    try:
        return RESOLUTION_TO_TIMEFRAME[resolution]
    except KeyError as exc:
        raise SourcePreconditionError(
            f"unsupported chart resolution {resolution!r}",
        ) from exc


def _parse_timestamp(value: Any) -> datetime:
    """Deterministically interpret a bar ``time`` field as Unix time.

    The production timestamp unit is not verified against a real response.
    This function uses a non-overlapping magnitude rule: values in the
    seconds range are treated as seconds; values in the milliseconds range
    are treated as milliseconds.  Values outside both ranges, or ambiguous
    malformed values, are rejected.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("timestamp must be numeric")

    if _EPOCH_SECONDS_MIN <= value <= _EPOCH_SECONDS_MAX:
        epoch_us = int(value) * 1_000_000
    elif _EPOCH_MILLIS_MIN <= value <= _EPOCH_MILLIS_MAX:
        epoch_us = int(value) * 1_000
    else:
        raise ValueError("timestamp is outside the accepted seconds/milliseconds range")

    try:
        utc_instant = datetime.fromtimestamp(epoch_us / 1_000_000, tz=timezone.utc)
    except (OverflowError, OSError, ValueError) as exc:
        raise ValueError(f"invalid timestamp value: {exc}") from exc

    return to_new_york(utc_instant)


def _parse_price(value: Any, field: str) -> Decimal:
    try:
        return as_decimal(value, field)
    except (TypeError, ValueError) as exc:
        raise MarketDataValidationError(
            f"invalid price for {field}: {exc}",
            code="INVALID_PRICE",
        ) from exc


class TvMarketDataSource:
    """TradingView market-data adapter backed by an injected MCP client.

    The injected client must provide the same interface used by the adapter:
    ``health()`` and ``call_tool(name, arguments)``.  The real
    ``McpStdioClient`` satisfies this interface; tests inject a fake.
    """

    name = _ADAPTER_NAME

    def __init__(
        self,
        instrument: InstrumentRef,
        binding: SourceBinding,
        client: Any,
        *,
        now: datetime | None = None,
    ) -> None:
        if not isinstance(instrument, InstrumentRef):
            raise TypeError("instrument must be an InstrumentRef")
        if not isinstance(binding, SourceBinding):
            raise TypeError("binding must be a SourceBinding")
        if binding.adapter.lower() != _ADAPTER_NAME:
            raise ValueError(
                f"TradingView adapter requires binding.adapter='tradingview', "
                f"got {binding.adapter!r}"
            )
        if not binding.is_verified:
            raise InstrumentNotMappedError(
                f"binding {binding.source_label!r} is not VERIFIED; "
                "unverified TradingView bindings are not used by the adapter",
                code="INSTRUMENT_NOT_MAPPED",
            )

        self._instrument = instrument
        self._binding = binding
        self._client = client
        self._now = _resolve_now(now)

    @property
    def instrument(self) -> InstrumentRef:
        return self._instrument

    @property
    def binding(self) -> SourceBinding:
        return self._binding

    @property
    def client(self) -> Any:
        return self._client

    def capabilities(self) -> MarketDataCapabilities:
        return MarketDataCapabilities(
            max_bars=_MAX_BARS,
            supports_date_range=False,
            provides_volume=True,
            completion_modes=(CompletionMode.EVIDENCE,),
        )

    def health(self) -> Any:
        """Delegate to the client health mechanism (tv_health_check)."""

        try:
            return self._client.health()
        except Exception as exc:
            raise SourceUnavailableError(
                f"TradingView source is unavailable: {exc}",
            ) from exc

    def get_candles(self, request: CandleRequest) -> CandleSeries:
        """Return a validated TradingView candle series for ``request``."""

        if not isinstance(request, CandleRequest):
            raise TypeError("request must be a CandleRequest")
        if request.instrument != self._instrument:
            raise InstrumentNotMappedError(
                f"adapter is bound to {self._instrument.canonical_id!r}, "
                f"not {request.instrument.canonical_id!r}",
                code="INSTRUMENT_NOT_MAPPED",
            )
        if request.source_filter is not None and (
            request.source_filter != self._binding.source_label
        ):
            raise InstrumentNotMappedError(
                f"source filter {request.source_filter!r} does not match the "
                f"bound source label {self._binding.source_label!r}",
                code="INSTRUMENT_NOT_MAPPED",
            )

        count = request.count if request.count is not None else _DEFAULT_COUNT
        if not isinstance(count, int) or isinstance(count, bool):
            raise MarketDataValidationError(
                "count must be an integer",
                code="MARKET_DATA_VALIDATION",
            )
        if not 1 <= count <= _MAX_BARS:
            raise MarketDataValidationError(
                f"count must be between 1 and {_MAX_BARS}",
                code="MARKET_DATA_VALIDATION",
            )

        # 1. Source availability check.
        try:
            self.health()
        except SourceUnavailableError:
            raise
        except Exception as exc:
            raise SourceUnavailableError(
                f"TradingView source health check failed: {exc}",
            ) from exc

        # 2. Chart state precondition check.
        chart_state = self._request_chart_state()

        # 3. Verify symbol and resolution before any OHLCV request.
        self._verify_chart_state(chart_state, request.timeframe)

        # 4. OHLCV retrieval (only after chart verification passes).
        bars_response = self._request_ohlcv(count)

        # 5. Normalize bars into candles.
        candles = self._normalize_bars(bars_response, request.timeframe)

        # 6. Apply evidence completion (incoming candles start incomplete).
        completed = apply_evidence_completion(candles)

        # 7. Validate the final series against the binding.
        validated = validate_candles(
            completed,
            instrument=self._instrument,
            binding=self._binding,
            timeframe=request.timeframe,
            now=self._now,
        )

        # 8. Return provider-independent series.
        fetched_at = self._now
        metadata = AdapterMetadata(
            adapter_name=_ADAPTER_NAME,
            adapter_version=_ADAPTER_VERSION,
            completion_mode=CompletionMode.EVIDENCE,
            sources=(self._binding.source_label,),
            details={
                "chart_symbol": chart_state.get("symbol", ""),
                "chart_resolution": str(chart_state.get("resolution", "")),
                "bar_count": str(len(validated)),
            },
        )
        return CandleSeries(
            instrument=self._instrument,
            timeframe=request.timeframe,
            candles=validated,
            fetched_at_utc=fetched_at,
            metadata=metadata,
        )

    # ------------------------------------------------------------------ internals

    def _parse_tool_result(
        self, result: Mapping[str, Any], tool_name: str
    ) -> Mapping[str, Any]:
        """Parse the JSON text inside an MCP tool result's content array.

        The real :class:`McpStdioClient` returns the raw JSON-RPC result, which
        wraps the payload in ``content[].text``.  This helper extracts and
        parses it so the adapter works with both the real client and test fakes
        that return either a raw mapping or an MCP-wrapped result.
        """

        if not isinstance(result, Mapping):
            raise SourcePreconditionError(
                f"{tool_name} result is not a mapping",
            )

        # A test fake may return the payload directly.
        if "symbol" in result or "bars" in result or "success" in result:
            return result

        content = result.get("content")
        if not isinstance(content, list) or not content:
            raise SourcePreconditionError(
                f"{tool_name} result has no content array",
            )
        first = content[0]
        if not isinstance(first, Mapping):
            raise SourcePreconditionError(
                f"{tool_name} content entry is not an object",
            )
        text = first.get("text")
        if not isinstance(text, str) or not text:
            raise SourcePreconditionError(
                f"{tool_name} result has no text content",
            )
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise SourcePreconditionError(
                f"{tool_name} returned non-JSON text: {exc}",
            ) from exc
        if not isinstance(payload, Mapping):
            raise SourcePreconditionError(
                f"{tool_name} payload is not a JSON object",
            )
        return payload

    def _request_chart_state(self) -> Mapping[str, Any]:
        result = self._client.call_tool("chart_get_state", {})
        return self._parse_tool_result(result, "chart_get_state")

    def _verify_chart_state(
        self,
        chart_state: Mapping[str, Any],
        requested_timeframe: Timeframe,
    ) -> None:
        symbol = chart_state.get("symbol")
        resolution = chart_state.get("resolution")

        if not isinstance(symbol, str) or not symbol.strip():
            raise SourcePreconditionError(
                "chart state is missing a valid symbol",
            )
        if symbol != self._binding.source_symbol:
            raise SourcePreconditionError(
                f"chart symbol {symbol!r} does not match bound source symbol "
                f"{self._binding.source_symbol!r}",
            )

        try:
            chart_timeframe = _map_resolution(resolution)
        except SourcePreconditionError:
            raise
        except Exception as exc:
            raise SourcePreconditionError(
                f"chart state resolution is invalid: {exc}",
            ) from exc

        if chart_timeframe != requested_timeframe:
            raise SourcePreconditionError(
                f"chart timeframe {chart_timeframe.value} does not match "
                f"requested timeframe {requested_timeframe.value}",
            )

    def _request_ohlcv(self, count: int) -> Mapping[str, Any]:
        result = self._client.call_tool(
            "data_get_ohlcv",
            {"count": count},
        )
        return self._parse_tool_result(result, "data_get_ohlcv")

    def _normalize_bars(
        self,
        response: Mapping[str, Any],
        requested_timeframe: Timeframe,
    ) -> tuple[Candle, ...]:
        bars = response.get("bars")
        if not isinstance(bars, list):
            raise MarketDataValidationError(
                "OHLCV response has no bars array",
                code="MARKET_DATA_VALIDATION",
            )
        if not bars:
            raise MarketDataValidationError(
                "OHLCV response contains no bars",
                code="MARKET_DATA_VALIDATION",
            )

        candles: list[Candle] = []
        for index, bar in enumerate(bars):
            if not isinstance(bar, Mapping):
                raise MarketDataValidationError(
                    f"bar {index} is not an object",
                    code="MARKET_DATA_VALIDATION",
                )

            try:
                open_value = _parse_price(bar.get("open"), "open")
                high_value = _parse_price(bar.get("high"), "high")
                low_value = _parse_price(bar.get("low"), "low")
                close_value = _parse_price(bar.get("close"), "close")
            except MarketDataValidationError:
                raise

            try:
                timestamp = _parse_timestamp(bar.get("time"))
            except ValueError as exc:
                raise MarketDataValidationError(
                    f"bar {index} has invalid timestamp: {exc}",
                    code="INVALID_TIMESTAMP",
                ) from exc

            volume = bar.get("volume", 0)
            try:
                volume_decimal = as_decimal(volume, "volume")
            except (TypeError, ValueError):
                volume_decimal = Decimal("0")

            candles.append(
                Candle(
                    timestamp=timestamp,
                    symbol=self._binding.source_symbol,
                    timeframe=requested_timeframe.value,
                    open=open_value,
                    high=high_value,
                    low=low_value,
                    close=close_value,
                    source=self._binding.source_label,
                    is_closed=False,
                )
            )

        return tuple(candles)
