import hashlib
import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from trading_automation.data.market_data import (
    AmbiguousSourceError,
    CandleRequest,
    CompletionMode,
    CsvMarketDataSource,
    InstrumentNotMappedError,
    InstrumentRef,
    MarketDataSource,
    MarketDataValidationError,
    SeriesValidationError,
    SourceBinding,
    Timeframe,
    UnsupportedRequestError,
)

NOW = datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)
HEADER = "timestamp,symbol,timeframe,open,high,low,close,source,is_closed\n"


def row(
    timestamp,
    *,
    symbol="EURUSD",
    timeframe="H1",
    open_value="1.10000000",
    high="1.12000000",
    low="1.09000000",
    close="1.11000000",
    source="TEST_FIXTURE",
    is_closed="true",
):
    return (
        f"{timestamp},{symbol},{timeframe},{open_value},{high},{low},{close},"
        f"{source},{is_closed}\n"
    )


def instrument():
    return InstrumentRef(
        canonical_id="EURUSD",
        asset_class="forex",
        display_name="Euro / US Dollar",
    )


def binding(**overrides):
    values = {
        "adapter": "csv",
        "source_symbol": "EURUSD",
        "source_label": "TEST_FIXTURE",
        "status": "CANDIDATE",
    }
    values.update(overrides)
    return SourceBinding(**values)


def adapter(path, **overrides):
    values = {
        "instrument": instrument(),
        "binding": binding(),
        "now": NOW,
    }
    values.update(overrides)
    return CsvMarketDataSource(path, **values)


def request(**overrides):
    values = {
        "instrument": instrument(),
        "timeframe": Timeframe.H1,
        "count": 10,
    }
    values.update(overrides)
    return CandleRequest(**values)


class CsvMarketDataSourceTests(unittest.TestCase):
    def _write(self, path, content):
        path.write_text(HEADER + content, encoding="utf-8")
        return path

    def test_reads_valid_csv_and_preserves_source_identity(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = self._write(
                Path(temp_dir) / "candles.csv",
                row("2026-11-02T09:00:00-05:00", is_closed="true")
                + row("2026-11-02T10:00:00-05:00", is_closed="false")
                + row("2026-11-02T11:00:00-05:00", is_closed="true"),
            )
            series = adapter(path).get_candles(request(count=3))

        self.assertEqual(series.instrument.canonical_id, "EURUSD")
        self.assertEqual(series.timeframe, Timeframe.H1)
        self.assertEqual(series.metadata.completion_mode, CompletionMode.DECLARED)
        self.assertEqual(series.metadata.sources, ("TEST_FIXTURE",))
        self.assertEqual([item.source for item in series.candles], ["TEST_FIXTURE"] * 3)
        self.assertEqual([item.symbol for item in series.candles], ["EURUSD"] * 3)

    def test_preserves_declared_is_closed_state_exactly(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = self._write(
                Path(temp_dir) / "candles.csv",
                row("2026-11-02T09:00:00-05:00", is_closed="true")
                + row("2026-11-02T10:00:00-05:00", is_closed="false")
                + row("2026-11-02T11:00:00-05:00", is_closed="true"),
            )
            series = adapter(path).get_candles(request(count=3))

        self.assertEqual(
            [item.is_closed for item in series.candles],
            [True, False, True],
        )

    def test_returns_most_recent_requested_count(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = self._write(
                Path(temp_dir) / "candles.csv",
                row("2026-11-02T09:00:00-05:00")
                + row("2026-11-02T10:00:00-05:00")
                + row("2026-11-02T11:00:00-05:00"),
            )
            series = adapter(path).get_candles(request(count=2))

        self.assertEqual(len(series.candles), 2)
        self.assertEqual(series.candles[-1].timestamp.hour, 11)
        self.assertEqual(series.candles[0].timestamp.hour, 10)

    def test_returns_available_candles_when_fewer_than_requested(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = self._write(
                Path(temp_dir) / "candles.csv",
                row("2026-11-02T09:00:00-05:00"),
            )
            series = adapter(path).get_candles(request(count=500))

        self.assertEqual(len(series.candles), 1)

    def test_preserves_eight_decimal_prices(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = self._write(
                Path(temp_dir) / "candles.csv",
                row(
                    "2026-11-02T09:00:00-05:00",
                    open_value="1.12345678",
                    high="1.23456789",
                    low="1.01234567",
                    close="1.19876543",
                ),
            )
            series = adapter(path).get_candles(request())

        candle = series.candles[0]
        self.assertEqual(candle.open, Decimal("1.12345678"))
        self.assertEqual(candle.high, Decimal("1.23456789"))
        self.assertEqual(candle.high.as_tuple().exponent, -8)
        self.assertEqual(candle.close, Decimal("1.19876543"))

    def test_rejects_mixed_sources_without_a_filter(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = self._write(
                Path(temp_dir) / "candles.csv",
                row("2026-11-02T09:00:00-05:00", source="TEST_FIXTURE")
                + row("2026-11-02T10:00:00-05:00", source="OTHER_FIXTURE"),
            )
            with self.assertRaises(AmbiguousSourceError):
                adapter(path).get_candles(request(count=2))

    def test_explicit_source_filter_selects_one_source(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = self._write(
                Path(temp_dir) / "candles.csv",
                row("2026-11-02T09:00:00-05:00", source="TEST_FIXTURE")
                + row("2026-11-02T10:00:00-05:00", source="OTHER_FIXTURE"),
            )
            series = adapter(path).get_candles(
                request(count=2, source_filter="TEST_FIXTURE")
            )

        self.assertEqual([item.source for item in series.candles], ["TEST_FIXTURE"])

    def test_source_filter_must_match_binding_label(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = self._write(
                Path(temp_dir) / "candles.csv",
                row("2026-11-02T09:00:00-05:00"),
            )
            with self.assertRaises(InstrumentNotMappedError):
                adapter(path).get_candles(
                    request(source_filter="OTHER_FIXTURE")
                )

    def test_binding_source_label_must_match_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = self._write(
                Path(temp_dir) / "candles.csv",
                row("2026-11-02T09:00:00-05:00", source="TEST_FIXTURE"),
            )
            with self.assertRaises(MarketDataValidationError) as context:
                adapter(
                    path,
                    binding=binding(source_label="OTHER_FIXTURE"),
                ).get_candles(request())
        self.assertEqual(context.exception.code, "SOURCE_BINDING_MISMATCH")

    def test_rejects_invalid_csv(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = self._write(
                Path(temp_dir) / "candles.csv",
                row(
                    "2026-11-02T09:00:00-05:00",
                    open_value="1.10",
                    high="1.00",
                    low="0.90",
                    close="1.15",
                ),
            )
            with self.assertRaises(MarketDataValidationError) as context:
                adapter(path).get_candles(request())
        self.assertEqual(context.exception.code, "CSV_VALIDATION_REJECTED")

    def test_rejects_missing_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source = adapter(Path(temp_dir) / "missing.csv")
            self.assertFalse(source.health().available)
            with self.assertRaises(MarketDataValidationError):
                source.get_candles(request())

    def test_health_reports_readable_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = self._write(
                Path(temp_dir) / "candles.csv",
                row("2026-11-02T09:00:00-05:00"),
            )
            health = adapter(path).health()

        self.assertTrue(health.available)
        self.assertIsNone(health.reason)

    def test_rejects_future_candle(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = self._write(
                Path(temp_dir) / "candles.csv",
                row("2030-01-02T09:00:00-05:00"),
            )
            with self.assertRaises(SeriesValidationError) as context:
                adapter(path).get_candles(request())
        self.assertIn(
            "FUTURE_CANDLE",
            {issue.code for issue in context.exception.issues},
        )

    def test_rejects_request_for_another_instrument(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = self._write(
                Path(temp_dir) / "candles.csv",
                row("2026-11-02T09:00:00-05:00"),
            )
            other = InstrumentRef(
                canonical_id="GBPUSD",
                asset_class="forex",
                display_name="British Pound / US Dollar",
            )
            with self.assertRaises(UnsupportedRequestError):
                adapter(path).get_candles(request(instrument=other))

    def test_rejects_no_matching_symbol(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = self._write(
                Path(temp_dir) / "candles.csv",
                row("2026-11-02T09:00:00-05:00", symbol="GBPUSD"),
            )
            with self.assertRaises(MarketDataValidationError) as context:
                adapter(path).get_candles(request())
        self.assertEqual(context.exception.code, "NO_MATCHING_CANDLES")

    def test_capabilities_are_declared_only(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = self._write(
                Path(temp_dir) / "candles.csv",
                row("2026-11-02T09:00:00-05:00"),
            )
            source = adapter(path)
            self.assertIsInstance(source, MarketDataSource)
            capabilities = source.capabilities()
        self.assertEqual(capabilities.max_bars, 500)
        self.assertFalse(capabilities.supports_date_range)
        self.assertFalse(capabilities.provides_volume)
        self.assertEqual(capabilities.completion_modes, (CompletionMode.DECLARED,))

    def test_metadata_contains_provenance_without_full_path(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = self._write(
                Path(temp_dir) / "candles.csv",
                row("2026-11-02T09:00:00-05:00"),
            )
            expected_sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
            series = adapter(path).get_candles(request())

        self.assertEqual(series.metadata.details["filename"], "candles.csv")
        self.assertNotIn(temp_dir, series.metadata.details["filename"])
        self.assertEqual(series.metadata.details["sha256"], expected_sha256)
        self.assertEqual(series.raw_payload_sha256, expected_sha256)

    def test_rejects_binding_for_another_adapter(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = self._write(
                Path(temp_dir) / "candles.csv",
                row("2026-11-02T09:00:00-05:00"),
            )
            with self.assertRaises(ValueError):
                adapter(path, binding=binding(adapter="tradingview"))

    def test_get_candles_does_not_create_a_database(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            path = self._write(
                root / "candles.csv",
                row("2026-11-02T09:00:00-05:00"),
            )
            adapter(path).get_candles(request())
            files = sorted(item.name for item in root.iterdir())

        self.assertEqual(files, ["candles.csv"])


if __name__ == "__main__":
    unittest.main()
