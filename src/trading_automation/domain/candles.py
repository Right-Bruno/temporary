"""Canonical OHLC candle model.

This is a data-integrity primitive, not a trading signal engine.
``Candle.timestamp`` is the candle's opening time, normalized to
``America/New_York`` as required by STS V1.0 §2.2.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import TypeAlias

from trading_automation.domain.timezones import to_new_york

DecimalInput: TypeAlias = Decimal | int | float | str


class CandleDirection(StrEnum):
    BULLISH = "bullish"
    BEARISH = "bearish"
    INDECISIVE = "indecisive"


def as_decimal(value: DecimalInput, field_name: str) -> Decimal:
    """Convert a numeric input to Decimal without binary-float arithmetic."""
    if isinstance(value, bool):
        raise TypeError(f"{field_name} must be numeric, not bool")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field_name} must be a valid decimal value") from exc
    if not result.is_finite():
        raise ValueError(f"{field_name} must be finite")
    return result


@dataclass(frozen=True, slots=True)
class Candle:
    """OHLC candle whose ``timestamp`` is the opening time in New York time."""

    timestamp: datetime
    symbol: str
    timeframe: str
    open: DecimalInput
    high: DecimalInput
    low: DecimalInput
    close: DecimalInput
    source: str
    is_closed: bool

    def __post_init__(self) -> None:
        if not isinstance(self.timestamp, datetime):
            raise TypeError("timestamp must be a datetime")
        if self.timestamp.tzinfo is None or self.timestamp.utcoffset() is None:
            raise ValueError("timestamp must be timezone-aware; naive timestamps are rejected")
        object.__setattr__(self, "timestamp", to_new_york(self.timestamp))
        for name in ("symbol", "timeframe", "source"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        if not isinstance(self.is_closed, bool):
            raise TypeError("is_closed must be an explicit bool")

        for name in ("open", "high", "low", "close"):
            object.__setattr__(self, name, as_decimal(getattr(self, name), name))

        if self.high < max(self.open, self.close):
            raise ValueError("high must be greater than or equal to both open and close")
        if self.low > min(self.open, self.close):
            raise ValueError("low must be less than or equal to both open and close")
        if self.high < self.low:
            raise ValueError("high must be greater than or equal to low")

    @property
    def open_time(self) -> datetime:
        """Return the candle's opening time in ``America/New_York``."""
        return self.timestamp

    @property
    def direction(self) -> CandleDirection:
        """Return the mechanical Open/Close direction defined in STS §4.2."""
        if self.close > self.open:
            return CandleDirection.BULLISH
        if self.close < self.open:
            return CandleDirection.BEARISH
        return CandleDirection.INDECISIVE
