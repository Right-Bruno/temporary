import unittest
from datetime import datetime, timezone
from decimal import Decimal

from trading_automation.data.market_data import (
    CompletionMode,
    MarketDataValidationError,
    UnsupportedRequestError,
    apply_completion,
    apply_declared_completion,
    apply_evidence_completion,
)
from trading_automation.domain.candles import Candle


def candle(hour, *, is_closed, symbol="EURUSD", source="TEST_FIXTURE"):
    return Candle(
        timestamp=datetime(2026, 11, 2, hour, 0, tzinfo=timezone.utc),
        symbol=symbol,
        timeframe="H1",
        open="1.12345678",
        high="1.23456789",
        low="1.01234567",
        close="1.19876543",
        source=source,
        is_closed=is_closed,
    )


class EvidenceCompletionTests(unittest.TestCase):
    def test_later_candle_proves_completion_of_all_earlier_candles(self):
        candles = [
            candle(9, is_closed=False),
            candle(10, is_closed=False),
            candle(11, is_closed=False),
            candle(12, is_closed=False),
        ]

        result = apply_evidence_completion(candles)

        self.assertEqual([item.is_closed for item in result], [True, True, True, False])

    def test_final_candle_is_never_upgraded_by_age(self):
        old = Candle(
            timestamp=datetime(2020, 1, 2, 9, 0, tzinfo=timezone.utc),
            symbol="EURUSD",
            timeframe="D1",
            open="1.0",
            high="1.1",
            low="0.9",
            close="1.05",
            source="TEST_FIXTURE",
            is_closed=False,
        )

        result = apply_evidence_completion([old])

        self.assertFalse(result[0].is_closed)
        self.assertIs(result[0], old)

    def test_completed_candles_are_never_downgraded(self):
        candles = [
            candle(9, is_closed=True),
            candle(10, is_closed=True),
            candle(11, is_closed=False),
        ]

        result = apply_evidence_completion(candles)

        self.assertEqual([item.is_closed for item in result], [True, True, False])

    def test_declared_complete_final_candle_stays_complete(self):
        candles = [candle(9, is_closed=False), candle(10, is_closed=True)]

        result = apply_evidence_completion(candles)

        self.assertTrue(result[-1].is_closed)

    def test_preserves_identity_and_decimal_prices(self):
        original = candle(9, is_closed=False)
        result = apply_evidence_completion([original, candle(10, is_closed=False)])

        upgraded = result[0]
        self.assertTrue(upgraded.is_closed)
        self.assertEqual(upgraded.timestamp, original.timestamp)
        self.assertEqual(upgraded.symbol, original.symbol)
        self.assertEqual(upgraded.timeframe, original.timeframe)
        self.assertEqual(upgraded.source, original.source)
        self.assertEqual(upgraded.high, Decimal("1.23456789"))
        self.assertEqual(upgraded.high.as_tuple().exponent, -8)

    def test_empty_series_returns_empty(self):
        self.assertEqual(apply_evidence_completion([]), ())

    def test_rejects_mixed_series_as_evidence(self):
        with self.assertRaisesRegex(MarketDataValidationError, "one symbol"):
            apply_evidence_completion(
                [candle(9, is_closed=False, symbol="EURUSD"),
                 candle(10, is_closed=False, symbol="GBPUSD")]
            )
        with self.assertRaisesRegex(MarketDataValidationError, "one symbol"):
            apply_evidence_completion(
                [candle(9, is_closed=False, source="A"),
                 candle(10, is_closed=False, source="B")]
            )

    def test_rejects_unordered_or_duplicate_series(self):
        with self.assertRaisesRegex(MarketDataValidationError, "ascending"):
            apply_evidence_completion(
                [candle(10, is_closed=False), candle(9, is_closed=False)]
            )
        with self.assertRaisesRegex(MarketDataValidationError, "unique"):
            apply_evidence_completion(
                [candle(9, is_closed=False), candle(9, is_closed=False)]
            )


class DeclaredAndDispatchCompletionTests(unittest.TestCase):
    def test_declared_mode_preserves_every_flag(self):
        candles = [
            candle(9, is_closed=False),
            candle(10, is_closed=True),
            candle(11, is_closed=False),
        ]

        result = apply_declared_completion(candles)

        self.assertEqual([item.is_closed for item in result], [False, True, False])

    def test_default_mode_is_evidence(self):
        candles = [candle(9, is_closed=False), candle(10, is_closed=False)]

        result = apply_completion(candles)

        self.assertTrue(result[0].is_closed)
        self.assertFalse(result[1].is_closed)

    def test_elapsed_mode_is_defined_but_not_implemented(self):
        with self.assertRaises(UnsupportedRequestError) as context:
            apply_completion([candle(9, is_closed=False)], CompletionMode.ELAPSED)
        self.assertEqual(context.exception.code, "COMPLETION_MODE_UNSUPPORTED")

    def test_unknown_mode_is_rejected(self):
        with self.assertRaises(ValueError):
            apply_completion([candle(9, is_closed=False)], "seven_day_rule")


if __name__ == "__main__":
    unittest.main()
