"""Candle-completion policy for the provider-independent market-data layer.

Approved policy (D-05)
----------------------
``CompletionMode.EVIDENCE`` is the default.  A candle is established as
completed only when a later candle in the same validated series exists.  The
final candle of a series therefore keeps its declared state: it is never
upgraded by age and never downgraded.

No elapsed-time rule, no seven-day D1 rule, no inferred session calendar, and
no completed-to-incomplete downgrade is implemented here.

``CompletionMode.DECLARED`` is used by the CSV adapter: the existing
``is_closed`` field is authoritative and is preserved exactly.

``CompletionMode.ELAPSED`` is defined by the contract for a future,
separately approved mode but is deliberately not implemented in Phase 1.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

from trading_automation.data.market_data.contract import (
    CompletionMode,
    MarketDataValidationError,
    UnsupportedRequestError,
)
from trading_automation.domain.candles import Candle
from trading_automation.domain.timezones import to_epoch_microseconds


def _as_candles(candles: Sequence[Candle]) -> tuple[Candle, ...]:
    values = tuple(candles)
    if any(not isinstance(candle, Candle) for candle in values):
        raise TypeError("candles must contain Candle instances")
    return values


def _ordered_single_series(candles: tuple[Candle, ...]) -> tuple[int, ...]:
    """Return epoch microseconds after enforcing a single ordered series.

    Evidence must never cross series boundaries, so mixed symbol, timeframe,
    or source labels are rejected rather than treated as proof of completion.
    """
    epochs: list[int] = []
    for candle in candles:
        epochs.append(to_epoch_microseconds(candle.timestamp))

    if len(set(epochs)) != len(epochs):
        raise MarketDataValidationError(
            "completion requires unique candle open times",
            code="DUPLICATE_CANDLE_IDENTITY",
        )
    if epochs != sorted(epochs):
        raise MarketDataValidationError(
            "completion requires strictly ascending candle open times",
            code="OUT_OF_ORDER_CANDLES",
        )

    symbols = {candle.symbol for candle in candles}
    timeframes = {candle.timeframe for candle in candles}
    sources = {candle.source for candle in candles}
    if len(symbols) > 1 or len(timeframes) > 1 or len(sources) > 1:
        raise MarketDataValidationError(
            "completion requires a series with one symbol, timeframe, and source",
            code="MIXED_SERIES",
        )
    return tuple(epochs)


def apply_evidence_completion(candles: Sequence[Candle]) -> tuple[Candle, ...]:
    """Apply D-05 evidence-only completion to an ordered series.

    Every candle except the last is established as completed because a later
    candle in the same series exists.  The last candle is returned unchanged,
    whether it was declared complete or incomplete.  Existing Decimal values
    and the source-native candle identity are preserved.
    """
    values = _as_candles(candles)
    if not values:
        return ()
    _ordered_single_series(values)

    completed: list[Candle] = []
    final_index = len(values) - 1
    for index, candle in enumerate(values):
        if index == final_index or candle.is_closed:
            completed.append(candle)
            continue
        completed.append(replace(candle, is_closed=True))
    return tuple(completed)


def apply_declared_completion(candles: Sequence[Candle]) -> tuple[Candle, ...]:
    """Return the series unchanged because the source declares completion."""
    return _as_candles(candles)


def apply_completion(
    candles: Sequence[Candle],
    mode: CompletionMode | str = CompletionMode.EVIDENCE,
) -> tuple[Candle, ...]:
    """Apply the requested completion mode.

    ``evidence`` is the default.  ``declared`` preserves the source's
    ``is_closed`` values.  ``elapsed`` exists in the contract but is not
    implemented in Phase 1 and fails closed with ``UnsupportedRequestError``.
    """
    try:
        resolved = mode if isinstance(mode, CompletionMode) else CompletionMode(mode)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"unsupported completion mode: {mode!r}") from exc

    if resolved is CompletionMode.EVIDENCE:
        return apply_evidence_completion(candles)
    if resolved is CompletionMode.DECLARED:
        return apply_declared_completion(candles)
    raise UnsupportedRequestError(
        "elapsed-time completion is not implemented in Phase 1; "
        "only evidence and declared modes are available",
        code="COMPLETION_MODE_UNSUPPORTED",
    )
