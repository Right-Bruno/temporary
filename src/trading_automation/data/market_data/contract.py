"""Provider-independent market-data contract.

This module defines the common request/response types used by every market
data adapter.  It deliberately contains no TradingView, MT5, CSV, SQLite, or
network specifics.

Identity rules
--------------
``InstrumentRef`` is the canonical, provider-independent instrument identity
used by the strategy layer.

``Candle`` keeps its existing source-native identity::

    (open_time_utc_us, candle.symbol, candle.timeframe, candle.source)

A :class:`CandleSeries` carries both: the canonical ``InstrumentRef`` and the
source-native candles.  It never rewrites ``Candle.symbol`` or
``Candle.source``.  Provider-native symbols live in :class:`SourceBinding`;
provider diagnostics live in :class:`AdapterMetadata`.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from types import MappingProxyType
from typing import Protocol, runtime_checkable

from trading_automation.domain.candles import Candle
from trading_automation.domain.timezones import to_epoch_microseconds

MAX_BARS_PER_REQUEST = 500

_CANONICAL_ID_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9_.-]*$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class Timeframe(StrEnum):
    """Timeframes recognised by the approved STS data contract."""

    M5 = "M5"
    H1 = "H1"
    D1 = "D1"


class BindingStatus(StrEnum):
    """Verification status of a provider binding.

    ``CANDIDATE`` and ``TO_VERIFY`` bindings are never treated as verified.
    """

    VERIFIED = "VERIFIED"
    CANDIDATE = "CANDIDATE"
    TO_VERIFY = "TO_VERIFY"


class CompletionMode(StrEnum):
    """How candle completion is established for a source.

    ``EVIDENCE``: a later candle in the same validated series proves that an
    earlier candle completed.  The final candle remains in its declared state.

    ``DECLARED``: the source explicitly declares ``is_closed`` for every
    candle (existing CSV behaviour).

    ``ELAPSED``: reserved for a future, separately approved elapsed-time mode.
    It is deliberately not implemented in Phase 1 Stages 1-2.
    """

    EVIDENCE = "evidence"
    DECLARED = "declared"
    ELAPSED = "elapsed"


class MarketDataError(Exception):
    """Base class for market-data contract failures."""

    code = "MARKET_DATA_ERROR"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code


class SourceUnavailableError(MarketDataError):
    """The configured source cannot currently provide data."""

    code = "SOURCE_UNAVAILABLE"


class InstrumentNotMappedError(MarketDataError):
    """No provider binding exists for the requested canonical instrument."""

    code = "INSTRUMENT_NOT_MAPPED"


class AmbiguousSourceError(MarketDataError):
    """More than one source or binding matches and no explicit filter was given."""

    code = "AMBIGUOUS_SOURCE"


class UnverifiedBindingError(MarketDataError):
    """A binding exists but has not been verified for this use."""

    code = "UNVERIFIED_BINDING"


class UnsupportedRequestError(MarketDataError):
    """The request is outside the adapter's supported capabilities."""

    code = "UNSUPPORTED_REQUEST"


class SourcePreconditionError(MarketDataError):
    """The source is reachable but its state does not match the request."""

    code = "SOURCE_PRECONDITION"


class MarketDataValidationError(MarketDataError):
    """A series, request, or source payload violated the data contract."""

    code = "MARKET_DATA_VALIDATION"


def _require_non_empty_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    if value != value.strip():
        raise ValueError(f"{field_name} must not have leading or trailing whitespace")
    return value


def _require_aware(value: object, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{field_name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware; naive timestamps are rejected")
    return value


@dataclass(frozen=True, slots=True)
class InstrumentRef:
    """Canonical instrument identity, independent of any provider."""

    canonical_id: str
    asset_class: str
    display_name: str

    def __post_init__(self) -> None:
        canonical_id = _require_non_empty_text(self.canonical_id, "canonical_id")
        if not _CANONICAL_ID_PATTERN.match(canonical_id):
            raise ValueError(
                "canonical_id must use uppercase letters, digits, '_', '.', or '-' "
                "and must start with a letter or digit"
            )
        object.__setattr__(self, "canonical_id", canonical_id)
        object.__setattr__(
            self,
            "asset_class",
            _require_non_empty_text(self.asset_class, "asset_class"),
        )
        object.__setattr__(
            self,
            "display_name",
            _require_non_empty_text(self.display_name, "display_name"),
        )


@dataclass(frozen=True, slots=True)
class SourceBinding:
    """One provider-specific binding for a canonical instrument.

    ``source_label`` is the exact provenance label assigned to candles from
    this binding.  It is deliberately distinct from:

    * ``adapter`` - the adapter family (``tradingview``, ``csv``);
    * the canonical instrument id;
    * ``source_symbol`` - the provider-native symbol/ticker.

    A binding never changes its own verification status.  Only an owner
    decision may promote ``CANDIDATE`` or ``TO_VERIFY`` to ``VERIFIED``.
    """

    adapter: str
    source_symbol: str
    source_label: str
    status: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "adapter",
            _require_non_empty_text(self.adapter, "adapter"),
        )
        object.__setattr__(
            self,
            "source_symbol",
            _require_non_empty_text(self.source_symbol, "source_symbol"),
        )
        object.__setattr__(
            self,
            "source_label",
            _require_non_empty_text(self.source_label, "source_label"),
        )
        status = _require_non_empty_text(self.status, "status").upper()
        if status not in {item.value for item in BindingStatus}:
            allowed = ", ".join(item.value for item in BindingStatus)
            raise ValueError(f"status must be one of: {allowed}")
        object.__setattr__(self, "status", status)

    @property
    def is_verified(self) -> bool:
        return self.status == BindingStatus.VERIFIED.value


@dataclass(frozen=True, slots=True)
class CandleRequest:
    """A provider-neutral request for the most recent candles."""

    instrument: InstrumentRef
    timeframe: Timeframe
    count: int
    source_filter: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.instrument, InstrumentRef):
            raise TypeError("instrument must be an InstrumentRef")
        if not isinstance(self.timeframe, Timeframe):
            try:
                object.__setattr__(self, "timeframe", Timeframe(self.timeframe))
            except (TypeError, ValueError) as exc:
                raise ValueError(f"unsupported timeframe: {self.timeframe!r}") from exc
        if not isinstance(self.count, int) or isinstance(self.count, bool):
            raise TypeError("count must be an integer")
        if not 1 <= self.count <= MAX_BARS_PER_REQUEST:
            raise ValueError(
                f"count must be between 1 and {MAX_BARS_PER_REQUEST}"
            )
        if self.source_filter is not None:
            object.__setattr__(
                self,
                "source_filter",
                _require_non_empty_text(self.source_filter, "source_filter"),
            )


@dataclass(frozen=True, slots=True)
class AdapterMetadata:
    """Generic provenance plus an opaque slot for provider diagnostics.

    ``details`` is adapter-specific and must never be required by the strategy
    layer.  TradingView chart symbol/resolution, for example, belongs here.
    """

    adapter_name: str
    adapter_version: str
    completion_mode: CompletionMode
    sources: tuple[str, ...]
    details: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "adapter_name",
            _require_non_empty_text(self.adapter_name, "adapter_name"),
        )
        object.__setattr__(
            self,
            "adapter_version",
            _require_non_empty_text(self.adapter_version, "adapter_version"),
        )
        if not isinstance(self.completion_mode, CompletionMode):
            try:
                object.__setattr__(
                    self,
                    "completion_mode",
                    CompletionMode(self.completion_mode),
                )
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"unsupported completion_mode: {self.completion_mode!r}"
                ) from exc
        sources = tuple(self.sources)
        if not sources:
            raise ValueError("sources must contain at least one source label")
        for source in sources:
            _require_non_empty_text(source, "source")
        if len(set(sources)) != len(sources):
            raise ValueError("sources must not contain duplicates")
        object.__setattr__(self, "sources", sources)

        details = dict(self.details or {})
        for key, value in details.items():
            _require_non_empty_text(key, "details key")
            if not isinstance(value, str):
                raise TypeError("details values must be strings")
        object.__setattr__(self, "details", MappingProxyType(details))


@dataclass(frozen=True, slots=True)
class CandleSeries:
    """A validated, chronologically ordered series from one source.

    The series requires exactly one source-native symbol and one source label.
    Adapters must reject mixed-source or mixed-symbol results rather than
    silently choosing one.
    """

    instrument: InstrumentRef
    timeframe: Timeframe
    candles: tuple[Candle, ...]
    fetched_at_utc: datetime
    metadata: AdapterMetadata
    raw_payload_sha256: str | None = None
    raw_payload_bytes: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.instrument, InstrumentRef):
            raise TypeError("instrument must be an InstrumentRef")
        if not isinstance(self.timeframe, Timeframe):
            try:
                object.__setattr__(self, "timeframe", Timeframe(self.timeframe))
            except (TypeError, ValueError) as exc:
                raise ValueError(f"unsupported timeframe: {self.timeframe!r}") from exc
        if not isinstance(self.metadata, AdapterMetadata):
            raise TypeError("metadata must be an AdapterMetadata")

        fetched_at = _require_aware(self.fetched_at_utc, "fetched_at_utc")
        object.__setattr__(self, "fetched_at_utc", fetched_at.astimezone(timezone.utc))

        candles = tuple(self.candles)
        if not candles:
            raise ValueError("candles must not be empty")
        if any(not isinstance(candle, Candle) for candle in candles):
            raise TypeError("candles must contain Candle instances")

        epochs: list[int] = []
        for candle in candles:
            if candle.timeframe != self.timeframe.value:
                raise ValueError(
                    f"candle timeframe {candle.timeframe!r} does not match "
                    f"series timeframe {self.timeframe.value!r}"
                )
            epochs.append(to_epoch_microseconds(candle.timestamp))
        if len(set(epochs)) != len(epochs):
            raise ValueError("candles must not contain duplicate open times")
        if epochs != sorted(epochs):
            raise ValueError("candles must be in strictly ascending open-time order")

        symbols = {candle.symbol for candle in candles}
        if len(symbols) != 1:
            raise ValueError("a series must contain exactly one source-native symbol")
        sources = {candle.source for candle in candles}
        if len(sources) != 1:
            raise ValueError("a series must contain exactly one source label")
        if set(self.metadata.sources) != sources:
            raise ValueError(
                "metadata.sources must match the source labels present in the series"
            )
        object.__setattr__(self, "candles", candles)

        if self.raw_payload_sha256 is not None:
            if not isinstance(self.raw_payload_sha256, str) or not _SHA256_PATTERN.match(
                self.raw_payload_sha256
            ):
                raise ValueError("raw_payload_sha256 must be a lowercase SHA-256 hex digest")
        if self.raw_payload_bytes is not None:
            if not isinstance(self.raw_payload_bytes, int) or isinstance(
                self.raw_payload_bytes, bool
            ):
                raise TypeError("raw_payload_bytes must be an integer")
            if self.raw_payload_bytes < 0:
                raise ValueError("raw_payload_bytes must not be negative")


@dataclass(frozen=True, slots=True)
class MarketDataCapabilities:
    """Static capabilities of an adapter."""

    max_bars: int
    supports_date_range: bool
    provides_volume: bool
    completion_modes: tuple[CompletionMode, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.max_bars, int) or isinstance(self.max_bars, bool):
            raise TypeError("max_bars must be an integer")
        if self.max_bars < 1:
            raise ValueError("max_bars must be at least 1")
        if not isinstance(self.supports_date_range, bool):
            raise TypeError("supports_date_range must be a bool")
        if not isinstance(self.provides_volume, bool):
            raise TypeError("provides_volume must be a bool")
        modes: list[CompletionMode] = []
        for mode in self.completion_modes:
            try:
                modes.append(mode if isinstance(mode, CompletionMode) else CompletionMode(mode))
            except (TypeError, ValueError) as exc:
                raise ValueError(f"unsupported completion mode: {mode!r}") from exc
        if not modes:
            raise ValueError("completion_modes must contain at least one mode")
        if len(set(modes)) != len(modes):
            raise ValueError("completion_modes must not contain duplicates")
        object.__setattr__(self, "completion_modes", tuple(modes))


@dataclass(frozen=True, slots=True)
class SourceHealth:
    """Result of a source availability check."""

    adapter_name: str
    available: bool
    checked_at_utc: datetime
    reason: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "adapter_name",
            _require_non_empty_text(self.adapter_name, "adapter_name"),
        )
        if not isinstance(self.available, bool):
            raise TypeError("available must be a bool")
        checked_at = _require_aware(self.checked_at_utc, "checked_at_utc")
        object.__setattr__(self, "checked_at_utc", checked_at.astimezone(timezone.utc))
        if not self.available and not (isinstance(self.reason, str) and self.reason.strip()):
            raise ValueError("unavailable health results must include a reason")
        if self.reason is not None:
            object.__setattr__(
                self,
                "reason",
                _require_non_empty_text(self.reason, "reason"),
            )


@runtime_checkable
class MarketDataSource(Protocol):
    """Common interface implemented by every market-data adapter."""

    name: str

    def capabilities(self) -> MarketDataCapabilities:
        """Return the adapter's static capabilities."""

    def health(self) -> SourceHealth:
        """Report whether the source is currently usable."""

    def get_candles(self, request: CandleRequest) -> CandleSeries:
        """Return a validated series for ``request`` or raise ``MarketDataError``."""
