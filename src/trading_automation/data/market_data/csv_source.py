"""CSV adapter for the provider-independent market-data interface.

The adapter reuses the existing, unchanged CSV validator.  It never writes to
SQLite, never downloads data, and never relabels a source.  Each candle keeps
the exact ``source`` and source-native ``symbol`` declared in the file.

CSV completion mode is ``declared``: the existing ``is_closed`` field is
authoritative and is preserved exactly.  Mixed-source results are rejected
unless the caller supplies an explicit source filter.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from trading_automation.data.csv_validation import validate_csv
from trading_automation.data.market_data.completion import apply_completion
from trading_automation.data.market_data.contract import (
    AdapterMetadata,
    AmbiguousSourceError,
    CandleRequest,
    CandleSeries,
    CompletionMode,
    InstrumentNotMappedError,
    InstrumentRef,
    MarketDataCapabilities,
    MarketDataValidationError,
    SourceBinding,
    SourceHealth,
    Timeframe,
    UnsupportedRequestError,
)
from trading_automation.data.market_data.validate import validate_candles
from trading_automation.domain.timezones import load_timezone

_ADAPTER_NAME = "csv"
_ADAPTER_VERSION = "1"
_MAX_BARS = 500


class CsvMarketDataSource:
    """Read one CSV file as a validated series for one source binding."""

    name = _ADAPTER_NAME

    def __init__(
        self,
        path: str | Path,
        *,
        instrument: InstrumentRef,
        binding: SourceBinding,
        source_timezone: str | None = None,
        now: datetime | None = None,
    ) -> None:
        if not isinstance(instrument, InstrumentRef):
            raise TypeError("instrument must be an InstrumentRef")
        if not isinstance(binding, SourceBinding):
            raise TypeError("binding must be a SourceBinding")
        if binding.adapter.lower() != _ADAPTER_NAME:
            raise ValueError(
                "CSV adapter requires a binding whose adapter is 'csv'"
            )
        if source_timezone is not None:
            load_timezone(source_timezone)
        if now is not None:
            if not isinstance(now, datetime):
                raise TypeError("now must be a datetime")
            if now.tzinfo is None or now.utcoffset() is None:
                raise ValueError("now must be timezone-aware")

        self._path = Path(path)
        self._instrument = instrument
        self._binding = binding
        self._source_timezone = source_timezone
        self._now = now

    @property
    def path(self) -> Path:
        return self._path

    def capabilities(self) -> MarketDataCapabilities:
        return MarketDataCapabilities(
            max_bars=_MAX_BARS,
            supports_date_range=False,
            provides_volume=False,
            completion_modes=(CompletionMode.DECLARED,),
        )

    def health(self) -> SourceHealth:
        checked_at = datetime.now(timezone.utc)
        try:
            with self._path.open("rb") as handle:
                handle.read(1)
        except OSError:
            return SourceHealth(
                adapter_name=_ADAPTER_NAME,
                available=False,
                checked_at_utc=checked_at,
                reason=f"CSV file is not readable: {self._path.name}",
            )
        return SourceHealth(
            adapter_name=_ADAPTER_NAME,
            available=True,
            checked_at_utc=checked_at,
        )

    def get_candles(self, request: CandleRequest) -> CandleSeries:
        if not isinstance(request, CandleRequest):
            raise TypeError("request must be a CandleRequest")
        if request.instrument != self._instrument:
            raise UnsupportedRequestError(
                f"CSV adapter is bound to {self._instrument.canonical_id!r}, "
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

        batch = validate_csv(self._path, source_timezone=self._source_timezone)
        if batch.report.status != "VALID":
            codes = ", ".join(issue.code for issue in batch.report.errors[:5])
            suffix = " ..." if len(batch.report.errors) > 5 else ""
            raise MarketDataValidationError(
                f"CSV validation rejected {batch.report.filename!r}"
                + (f": {codes}{suffix}" if codes else ""),
                code="CSV_VALIDATION_REJECTED",
            )

        timeframe = request.timeframe
        candidates = tuple(
            candle
            for candle in batch.candles
            if candle.symbol == self._binding.source_symbol
            and candle.timeframe == timeframe.value
        )
        if not candidates:
            raise MarketDataValidationError(
                f"no candles for {self._instrument.canonical_id!r} "
                f"{timeframe.value} and source symbol "
                f"{self._binding.source_symbol!r}",
                code="NO_MATCHING_CANDLES",
            )

        sources = {candle.source for candle in candidates}
        if request.source_filter is None:
            if len(sources) > 1:
                raise AmbiguousSourceError(
                    "CSV contains multiple sources for the requested series; "
                    "supply an explicit source filter: "
                    + ", ".join(sorted(sources)),
                    code="AMBIGUOUS_SOURCE",
                )
            selected_source = next(iter(sources))
            if selected_source != self._binding.source_label:
                raise MarketDataValidationError(
                    f"CSV source label {selected_source!r} does not match the "
                    f"bound source label {self._binding.source_label!r}",
                    code="SOURCE_BINDING_MISMATCH",
                )
        else:
            selected_source = request.source_filter
            if selected_source not in sources:
                raise MarketDataValidationError(
                    f"no candles for source label {selected_source!r}",
                    code="NO_MATCHING_CANDLES",
                )

        matching = tuple(
            candle for candle in candidates if candle.source == selected_source
        )
        selected = matching[-request.count :]

        declared = apply_completion(selected, CompletionMode.DECLARED)
        validated = validate_candles(
            declared,
            instrument=self._instrument,
            binding=self._binding,
            timeframe=timeframe,
            now=self._now,
        )

        fetched_at = self._now if self._now is not None else datetime.now(timezone.utc)
        metadata = AdapterMetadata(
            adapter_name=_ADAPTER_NAME,
            adapter_version=_ADAPTER_VERSION,
            completion_mode=CompletionMode.DECLARED,
            sources=(selected_source,),
            details={
                "filename": batch.report.filename,
                "sha256": batch.report.sha256 or "",
                "source_timezone": self._source_timezone or "",
                "validation_status": batch.report.status,
            },
        )
        return CandleSeries(
            instrument=self._instrument,
            timeframe=timeframe,
            candles=validated,
            fetched_at_utc=fetched_at,
            metadata=metadata,
            raw_payload_sha256=batch.report.sha256,
            raw_payload_bytes=batch.report.size_bytes,
        )


def supported_timeframes() -> tuple[Timeframe, ...]:
    """Return the timeframes accepted by the Phase 1 CSV adapter."""
    return (Timeframe.M5, Timeframe.H1, Timeframe.D1)
