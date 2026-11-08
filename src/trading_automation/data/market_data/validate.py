"""Structural and market-data validation for provider-independent series.

The existing :class:`~trading_automation.domain.candles.Candle` model already
rejects several impossible OHLC shapes.  This module validates a complete
series before it becomes a :class:`CandleSeries`, producing one deterministic
error listing every independently detectable problem.

No interpolation, missing-candle creation, gap classification, or conflicting
candle resolution is performed here.  Missing data remains unclassified.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from trading_automation.data.market_data.contract import (
    CandleSeries,
    InstrumentRef,
    MarketDataValidationError,
    SourceBinding,
    Timeframe,
)
from trading_automation.domain.candles import Candle, as_decimal
from trading_automation.domain.timezones import to_epoch_microseconds

_PRICE_FIELDS = ("open", "high", "low", "close")


@dataclass(frozen=True, slots=True)
class SeriesValidationIssue:
    """One series-contract violation."""

    code: str
    message: str
    candle_index: int | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "message": self.message,
            "candle_index": self.candle_index,
        }


class SeriesValidationError(MarketDataValidationError):
    """Raised when one or more series-contract checks fail."""

    def __init__(self, issues: Sequence[SeriesValidationIssue]) -> None:
        self.issues = tuple(issues)
        summary = "; ".join(
            f"{issue.code}: {issue.message}"
            + (
                f" (candle {issue.candle_index})"
                if issue.candle_index is not None
                else ""
            )
            for issue in self.issues
        )
        super().__init__(summary)


def _resolve_now(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    if not isinstance(now, datetime):
        raise TypeError("now must be a datetime")
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware; naive timestamps are rejected")
    return now.astimezone(timezone.utc)


def _resolve_timeframe(timeframe: Timeframe | str) -> Timeframe:
    try:
        return timeframe if isinstance(timeframe, Timeframe) else Timeframe(timeframe)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"unsupported timeframe: {timeframe!r}") from exc


def _decorate(index: int, message: str) -> str:
    return f"candle {index}: {message}"


def _validate_values(
    candles: Sequence[Candle],
    *,
    timeframe: Timeframe,
    expected_symbol: str | None,
    expected_source: str | None,
    now: datetime,
) -> list[SeriesValidationIssue]:
    issues: list[SeriesValidationIssue] = []
    now_us = to_epoch_microseconds(now)
    seen_identities: set[tuple[int, str, str, str]] = set()
    previous_epoch: int | None = None

    for index, candle in enumerate(candles):
        if not isinstance(candle, Candle):
            raise TypeError("candles must contain Candle instances")

        if not isinstance(candle.timestamp, datetime):
            issues.append(
                SeriesValidationIssue(
                    "INVALID_TIMESTAMP",
                    _decorate(index, "timestamp must be a datetime"),
                    index,
                )
            )
        elif candle.timestamp.tzinfo is None or candle.timestamp.utcoffset() is None:
            issues.append(
                SeriesValidationIssue(
                    "INVALID_TIMESTAMP",
                    _decorate(index, "timestamp must be timezone-aware"),
                    index,
                )
            )

        if candle.timeframe != timeframe.value:
            issues.append(
                SeriesValidationIssue(
                    "TIMEFRAME_MISMATCH",
                    _decorate(
                        index,
                        f"candle timeframe {candle.timeframe!r} does not match "
                        f"series timeframe {timeframe.value!r}",
                    ),
                    index,
                )
            )

        if expected_symbol is not None and candle.symbol != expected_symbol:
            issues.append(
                SeriesValidationIssue(
                    "SYMBOL_MISMATCH",
                    _decorate(
                        index,
                        f"candle symbol {candle.symbol!r} does not match the "
                        f"bound source symbol {expected_symbol!r}",
                    ),
                    index,
                )
            )

        if expected_source is not None and candle.source != expected_source:
            issues.append(
                SeriesValidationIssue(
                    "SOURCE_MISMATCH",
                    _decorate(
                        index,
                        f"candle source {candle.source!r} does not match the "
                        f"bound source label {expected_source!r}",
                    ),
                    index,
                )
            )

        prices: dict[str, Decimal] = {}
        for field in _PRICE_FIELDS:
            value = getattr(candle, field, None)
            if value is None:
                issues.append(
                    SeriesValidationIssue(
                        "REQUIRED_VALUE_MISSING",
                        _decorate(index, f"{field} is required"),
                        index,
                    )
                )
                continue
            try:
                parsed = as_decimal(value, field)
            except (TypeError, ValueError) as exc:
                issues.append(
                    SeriesValidationIssue(
                        "INVALID_PRICE",
                        _decorate(index, f"{field}: {exc}"),
                        index,
                    )
                )
                continue
            prices[field] = parsed
            if parsed <= 0:
                issues.append(
                    SeriesValidationIssue(
                        "NON_POSITIVE_PRICE",
                        _decorate(index, f"{field} must be positive"),
                        index,
                    )
                )

        if len(prices) == len(_PRICE_FIELDS):
            open_value = prices["open"]
            high_value = prices["high"]
            low_value = prices["low"]
            close_value = prices["close"]
            if high_value < max(open_value, close_value):
                issues.append(
                    SeriesValidationIssue(
                        "OHLC_INCONSISTENT",
                        _decorate(
                            index,
                            "high must be greater than or equal to both open and close",
                        ),
                        index,
                    )
                )
            if low_value > min(open_value, close_value):
                issues.append(
                    SeriesValidationIssue(
                        "OHLC_INCONSISTENT",
                        _decorate(
                            index,
                            "low must be less than or equal to both open and close",
                        ),
                        index,
                    )
                )
            if high_value < low_value:
                issues.append(
                    SeriesValidationIssue(
                        "OHLC_INCONSISTENT",
                        _decorate(
                            index,
                            "high must be greater than or equal to low",
                        ),
                        index,
                    )
                )

        if isinstance(candle.timestamp, datetime) and (
            candle.timestamp.tzinfo is not None
            and candle.timestamp.utcoffset() is not None
        ):
            epoch_us = to_epoch_microseconds(candle.timestamp)
            identity = (epoch_us, candle.symbol, candle.timeframe, candle.source)
            if identity in seen_identities:
                issues.append(
                    SeriesValidationIssue(
                        "DUPLICATE_CANDLE_IDENTITY",
                        _decorate(
                            index,
                            "duplicate candle identity "
                            f"(open_time_utc_us={epoch_us}, "
                            f"symbol={candle.symbol!r}, "
                            f"timeframe={candle.timeframe!r}, "
                            f"source={candle.source!r})",
                        ),
                        index,
                    )
                )
            seen_identities.add(identity)

            if epoch_us > now_us:
                issues.append(
                    SeriesValidationIssue(
                        "FUTURE_CANDLE",
                        _decorate(
                            index,
                            "candle opening time is later than the validation instant",
                        ),
                        index,
                    )
                )

            if previous_epoch is not None and epoch_us <= previous_epoch:
                issues.append(
                    SeriesValidationIssue(
                        "OUT_OF_ORDER_CANDLES",
                        _decorate(
                            index,
                            "candle open times must be strictly ascending",
                        ),
                        index,
                    )
                )
            previous_epoch = epoch_us

    return issues


def validate_candles(
    candles: Sequence[Candle],
    *,
    instrument: InstrumentRef,
    binding: SourceBinding,
    timeframe: Timeframe | str,
    now: datetime | None = None,
) -> tuple[Candle, ...]:
    """Validate candles against the canonical instrument's source binding.

    The binding is the mapping authority supplied by the symbol registry: the
    adapter name, provider-native symbol, and exact provenance label must all
    match the candles.  The canonical instrument id is carried separately and
    never replaces the source-native ``Candle.symbol``.
    """
    if not isinstance(instrument, InstrumentRef):
        raise TypeError("instrument must be an InstrumentRef")
    if not isinstance(binding, SourceBinding):
        raise TypeError("binding must be a SourceBinding")
    resolved_timeframe = _resolve_timeframe(timeframe)
    resolved_now = _resolve_now(now)

    values = tuple(candles)
    if not values:
        raise SeriesValidationError(
            (SeriesValidationIssue("EMPTY_SERIES", "series must not be empty"),)
        )

    issues = _validate_values(
        values,
        timeframe=resolved_timeframe,
        expected_symbol=binding.source_symbol,
        expected_source=binding.source_label,
        now=resolved_now,
    )
    if issues:
        raise SeriesValidationError(issues)
    return values


def validate_series(
    series: CandleSeries,
    *,
    now: datetime | None = None,
) -> CandleSeries:
    """Re-validate an already constructed ``CandleSeries``.

    Structural identity (single symbol, single source, ordering, uniqueness,
    timeframe) is enforced by ``CandleSeries`` itself.  This function performs
    the market-data checks that require a validation instant or price rules.
    """
    if not isinstance(series, CandleSeries):
        raise TypeError("series must be a CandleSeries")
    resolved_now = _resolve_now(now)
    issues = _validate_values(
        series.candles,
        timeframe=series.timeframe,
        expected_symbol=None,
        expected_source=None,
        now=resolved_now,
    )
    if issues:
        raise SeriesValidationError(issues)
    return series
