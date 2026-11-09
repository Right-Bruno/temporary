import unittest
from datetime import datetime, timezone
from decimal import Decimal

from trading_automation.data.market_data import (
    AdapterMetadata,
    CandleSeries,
    CompletionMode,
    InstrumentRef,
    MarketDataValidationError,
    SeriesValidationError,
    SourceBinding,
    Timeframe,
    validate_candles,
    validate_series,
)
from trading_automation.domain.candles import Candle

NOW = datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)


def instrument():
    return InstrumentRef(
        canonical_id="EURUSD",
        asset_class="forex",
        display_name="Euro / US Dollar",
    )


def binding():
    return SourceBinding(
        adapter="csv",
        source_symbol="EURUSD",
        source_label="TEST_FIXTURE",
        status="CANDIDATE",
    )


def candle(hour, **overrides):
    values = {
        "timestamp": datetime(2026, 11, 2, hour, 0, tzinfo=timezone.utc),
        "symbol": "EURUSD",
        "timeframe": "H1",
        "open": "1.10000000",
        "high": "1.12000000",
        "low": "1.09000000",
        "close": "1.11000000",
        "source": "TEST_FIXTURE",
        "is_closed": True,
    }
    values.update(overrides)
    return Candle(**values)


def issues_for(candles, **overrides):
    values = {
        "instrument": instrument(),
        "binding": binding(),
        "timeframe": Timeframe.H1,
        "now": NOW,
    }
    values.update(overrides)
    try:
        validate_candles(candles, **values)
    except SeriesValidationError as exc:
        return {issue.code for issue in exc.issues}
    raise AssertionError("expected SeriesValidationError")


class ValidateCandlesTests(unittest.TestCase):
    def test_accepts_a_valid_series_without_modification(self):
        candles = (candle(9), candle(10))

        result = validate_candles(
            candles,
            instrument=instrument(),
            binding=binding(),
            timeframe=Timeframe.H1,
            now=NOW,
        )

        self.assertEqual(result, candles)
        self.assertIsInstance(result, tuple)

    def test_rejects_wrong_canonical_source_symbol_or_label(self):
        self.assertIn(
            "SYMBOL_MISMATCH",
            issues_for([candle(9, symbol="EURUSDX")]),
        )
        self.assertIn(
            "SOURCE_MISMATCH",
            issues_for([candle(9, source="OTHER_SOURCE")]),
        )

    def test_rejects_timeframe_mismatch(self):
        self.assertIn(
            "TIMEFRAME_MISMATCH",
            issues_for([candle(9, timeframe="M5")]),
        )

    def test_rejects_unordered_and_duplicate_candles(self):
        self.assertIn("OUT_OF_ORDER_CANDLES", issues_for([candle(10), candle(9)]))
        duplicate_codes = issues_for([candle(9), candle(9)])
        self.assertIn("DUPLICATE_CANDLE_IDENTITY", duplicate_codes)

    def test_rejects_future_candles(self):
        future = candle(9, timestamp=datetime(2030, 1, 2, 9, 0, tzinfo=timezone.utc))
        self.assertIn("FUTURE_CANDLE", issues_for([future]))

    def test_candle_opening_exactly_at_validation_instant_is_not_future(self):
        exact = candle(9, timestamp=NOW)
        result = validate_candles(
            [exact],
            instrument=instrument(),
            binding=binding(),
            timeframe=Timeframe.H1,
            now=NOW,
        )
        self.assertEqual(result, (exact,))

    def test_rejects_non_positive_prices(self):
        value = candle(9)
        object.__setattr__(value, "open", Decimal("0"))
        self.assertIn("NON_POSITIVE_PRICE", issues_for([value]))

    def test_rejects_non_finite_prices(self):
        value = candle(9)
        object.__setattr__(value, "high", Decimal("NaN"))
        self.assertIn("INVALID_PRICE", issues_for([value]))

    def test_rejects_inconsistent_ohlc_values(self):
        value = candle(9)
        object.__setattr__(value, "high", Decimal("1.0"))
        self.assertIn("OHLC_INCONSISTENT", issues_for([value]))

    def test_rejects_naive_timestamp(self):
        value = candle(9)
        object.__setattr__(value, "timestamp", datetime(2026, 11, 2, 9, 0))
        self.assertIn("INVALID_TIMESTAMP", issues_for([value]))

    def test_rejects_empty_series(self):
        with self.assertRaises(SeriesValidationError) as context:
            validate_candles(
                [],
                instrument=instrument(),
                binding=binding(),
                timeframe=Timeframe.H1,
                now=NOW,
            )
        self.assertEqual(context.exception.issues[0].code, "EMPTY_SERIES")

    def test_rejects_naive_validation_instant(self):
        with self.assertRaises(ValueError):
            validate_candles(
                [candle(9)],
                instrument=instrument(),
                binding=binding(),
                timeframe=Timeframe.H1,
                now=datetime(2026, 11, 8, 12, 0),
            )

    def test_does_not_interpolate_missing_candles(self):
        candles = (candle(9), candle(13))
        result = validate_candles(
            candles,
            instrument=instrument(),
            binding=binding(),
            timeframe=Timeframe.H1,
            now=NOW,
        )
        self.assertEqual(len(result), 2)

    def test_errors_are_collected_not_hidden(self):
        value = candle(9, symbol="EURUSDX", source="OTHER_SOURCE")
        with self.assertRaises(SeriesValidationError) as context:
            validate_candles(
                [value],
                instrument=instrument(),
                binding=binding(),
                timeframe=Timeframe.H1,
                now=NOW,
            )
        codes = {issue.code for issue in context.exception.issues}
        self.assertIn("SYMBOL_MISMATCH", codes)
        self.assertIn("SOURCE_MISMATCH", codes)
        self.assertIn("SYMBOL_MISMATCH", str(context.exception))


class ValidateSeriesTests(unittest.TestCase):
    def _series(self, candles):
        return CandleSeries(
            instrument=instrument(),
            timeframe=Timeframe.H1,
            candles=tuple(candles),
            fetched_at_utc=NOW,
            metadata=AdapterMetadata(
                adapter_name="csv",
                adapter_version="1",
                completion_mode=CompletionMode.DECLARED,
                sources=("TEST_FIXTURE",),
            ),
        )

    def test_accepts_a_valid_series(self):
        series = self._series([candle(9), candle(10)])
        self.assertIs(validate_series(series, now=NOW), series)

    def test_revalidates_market_data_checks_after_construction(self):
        series = self._series([candle(9)])
        object.__setattr__(
            series.candles[0],
            "timestamp",
            datetime(2030, 1, 2, 9, 0, tzinfo=timezone.utc),
        )
        with self.assertRaises(SeriesValidationError) as context:
            validate_series(series, now=NOW)
        self.assertIn(
            "FUTURE_CANDLE",
            {issue.code for issue in context.exception.issues},
        )

    def test_rejects_non_series(self):
        with self.assertRaises(TypeError):
            validate_series(object(), now=NOW)


if __name__ == "__main__":
    unittest.main()
