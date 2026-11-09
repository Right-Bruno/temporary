import unittest
from datetime import datetime, timezone
from decimal import Decimal
from types import MappingProxyType

from trading_automation.data.market_data import (
    AdapterMetadata,
    BindingStatus,
    CandleRequest,
    CandleSeries,
    CompletionMode,
    InstrumentRef,
    MarketDataCapabilities,
    MarketDataSource,
    SourceBinding,
    SourceHealth,
    Timeframe,
)
from trading_automation.domain.candles import Candle


def make_instrument(**overrides):
    values = {
        "canonical_id": "EURUSD",
        "asset_class": "forex",
        "display_name": "Euro / US Dollar",
    }
    values.update(overrides)
    return InstrumentRef(**values)


def make_binding(**overrides):
    values = {
        "adapter": "tradingview",
        "source_symbol": "FOREXCOM:EURUSD",
        "source_label": "tradingview-mcp:FOREXCOM",
        "status": "CANDIDATE",
    }
    values.update(overrides)
    return SourceBinding(**values)


def make_candle(**overrides):
    values = {
        "timestamp": datetime(2026, 11, 2, 14, 0, tzinfo=timezone.utc),
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


def make_metadata(**overrides):
    values = {
        "adapter_name": "csv",
        "adapter_version": "1",
        "completion_mode": CompletionMode.DECLARED,
        "sources": ("TEST_FIXTURE",),
        "details": {"filename": "candles.csv"},
    }
    values.update(overrides)
    return AdapterMetadata(**values)


class InstrumentRefTests(unittest.TestCase):
    def test_accepts_canonical_provider_independent_identity(self):
        instrument = make_instrument()
        self.assertEqual(instrument.canonical_id, "EURUSD")
        self.assertEqual(instrument.asset_class, "forex")

    def test_rejects_invalid_canonical_ids(self):
        for value in ("", "eurusd", "-EURUSD", "EUR USD", " EURUSD"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    make_instrument(canonical_id=value)

    def test_rejects_empty_display_fields(self):
        with self.assertRaises(ValueError):
            make_instrument(asset_class=" ")
        with self.assertRaises(ValueError):
            make_instrument(display_name="")


class SourceBindingTests(unittest.TestCase):
    def test_preserves_provider_identity_separately_from_canonical_id(self):
        binding = make_binding()
        self.assertNotEqual(binding.source_symbol, "EURUSD")
        self.assertNotEqual(binding.source_label, "EURUSD")
        self.assertNotEqual(binding.adapter, binding.source_symbol)
        self.assertFalse(binding.is_verified)

    def test_status_is_explicit_and_normalised(self):
        self.assertEqual(make_binding(status="candidate").status, "CANDIDATE")
        self.assertTrue(make_binding(status="verified").is_verified)
        with self.assertRaises(ValueError):
            make_binding(status="TRUST_ME")

    def test_rejects_blank_fields(self):
        for field in ("adapter", "source_symbol", "source_label", "status"):
            with self.subTest(field=field):
                with self.assertRaises(ValueError):
                    make_binding(**{field: "  "})


class CandleRequestTests(unittest.TestCase):
    def test_accepts_supported_timeframe_and_count(self):
        request = CandleRequest(
            instrument=make_instrument(),
            timeframe="H1",
            count=500,
        )
        self.assertIs(request.timeframe, Timeframe.H1)
        self.assertEqual(request.count, 500)

    def test_rejects_unsupported_timeframe(self):
        with self.assertRaises(ValueError):
            CandleRequest(instrument=make_instrument(), timeframe="H4", count=1)

    def test_rejects_out_of_bounds_or_boolean_count(self):
        for value in (0, 501, True, 1.5):
            with self.subTest(value=value):
                with self.assertRaises((TypeError, ValueError)):
                    CandleRequest(
                        instrument=make_instrument(),
                        timeframe="H1",
                        count=value,
                    )

    def test_source_filter_is_optional_but_not_blank(self):
        request = CandleRequest(
            instrument=make_instrument(),
            timeframe="H1",
            count=1,
            source_filter="TEST_FIXTURE",
        )
        self.assertEqual(request.source_filter, "TEST_FIXTURE")
        with self.assertRaises(ValueError):
            CandleRequest(
                instrument=make_instrument(),
                timeframe="H1",
                count=1,
                source_filter=" ",
            )


class AdapterMetadataTests(unittest.TestCase):
    def test_sources_and_details_are_immutable(self):
        metadata = make_metadata()
        self.assertIsInstance(metadata.details, MappingProxyType)
        with self.assertRaises(TypeError):
            metadata.details["other"] = "value"

    def test_rejects_duplicate_or_empty_sources(self):
        with self.assertRaises(ValueError):
            make_metadata(sources=())
        with self.assertRaises(ValueError):
            make_metadata(sources=("TEST_FIXTURE", "TEST_FIXTURE"))

    def test_rejects_unknown_completion_mode(self):
        with self.assertRaises(ValueError):
            make_metadata(completion_mode="wall_clock")


class CandleSeriesTests(unittest.TestCase):
    def _series(self, candles, **overrides):
        values = {
            "instrument": make_instrument(),
            "timeframe": Timeframe.H1,
            "candles": tuple(candles),
            "fetched_at_utc": datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc),
            "metadata": make_metadata(),
        }
        values.update(overrides)
        return CandleSeries(**values)

    def test_accepts_one_ordered_series(self):
        series = self._series([make_candle()])
        self.assertEqual(series.candles[0].source, "TEST_FIXTURE")
        self.assertEqual(series.instrument.canonical_id, "EURUSD")

    def test_preserves_eight_decimal_prices_without_binary_round_trip(self):
        candle = make_candle(
            open="1.12345678",
            high="1.23456789",
            low="1.01234567",
            close="1.19876543",
        )
        series = self._series([candle])
        stored = series.candles[0]
        self.assertEqual(stored.open, Decimal("1.12345678"))
        self.assertEqual(stored.high, Decimal("1.23456789"))
        self.assertEqual(stored.low, Decimal("1.01234567"))
        self.assertEqual(stored.close, Decimal("1.19876543"))
        self.assertEqual(stored.high.as_tuple().exponent, -8)

    def test_rejects_empty_series(self):
        with self.assertRaises(ValueError):
            self._series([])

    def test_rejects_mixed_symbols(self):
        with self.assertRaisesRegex(ValueError, "one source-native symbol"):
            self._series(
                [
                    make_candle(),
                    make_candle(
                        timestamp=datetime(2026, 11, 2, 15, 0, tzinfo=timezone.utc),
                        symbol="GBPUSD",
                    ),
                ]
            )

    def test_rejects_mixed_sources(self):
        with self.assertRaisesRegex(ValueError, "one source label"):
            self._series(
                [
                    make_candle(),
                    make_candle(
                        timestamp=datetime(2026, 11, 2, 15, 0, tzinfo=timezone.utc),
                        source="OTHER_SOURCE",
                    ),
                ]
            )

    def test_rejects_duplicate_open_times(self):
        with self.assertRaisesRegex(ValueError, "duplicate open times"):
            self._series([make_candle(), make_candle()])

    def test_rejects_out_of_order_candles(self):
        later = make_candle(
            timestamp=datetime(2026, 11, 2, 15, 0, tzinfo=timezone.utc)
        )
        earlier = make_candle()
        with self.assertRaisesRegex(ValueError, "ascending"):
            self._series([later, earlier])

    def test_rejects_timeframe_mismatch(self):
        with self.assertRaisesRegex(ValueError, "does not match series timeframe"):
            self._series([make_candle()], timeframe=Timeframe.M5)

    def test_metadata_sources_must_match_candles(self):
        with self.assertRaisesRegex(ValueError, "metadata.sources"):
            self._series(
                [make_candle()],
                metadata=make_metadata(sources=("OTHER_SOURCE",)),
            )

    def test_raw_payload_metadata_is_validated(self):
        series = self._series(
            [make_candle()],
            raw_payload_sha256="a" * 64,
            raw_payload_bytes=10,
        )
        self.assertEqual(series.raw_payload_bytes, 10)
        with self.assertRaises(ValueError):
            self._series([make_candle()], raw_payload_sha256="not-a-digest")


class CapabilityAndHealthTests(unittest.TestCase):
    def test_capabilities_reject_duplicate_modes(self):
        with self.assertRaises(ValueError):
            MarketDataCapabilities(
                max_bars=500,
                supports_date_range=False,
                provides_volume=False,
                completion_modes=(CompletionMode.DECLARED, "declared"),
            )

    def test_unavailable_health_requires_reason(self):
        with self.assertRaises(ValueError):
            SourceHealth(
                adapter_name="csv",
                available=False,
                checked_at_utc=datetime(2026, 11, 8, tzinfo=timezone.utc),
            )
        health = SourceHealth(
            adapter_name="csv",
            available=False,
            checked_at_utc=datetime(2026, 11, 8, tzinfo=timezone.utc),
            reason="file missing",
        )
        self.assertFalse(health.available)

    def test_protocol_is_runtime_checkable(self):
        class Dummy:
            name = "dummy"

            def capabilities(self):
                raise NotImplementedError

            def health(self):
                raise NotImplementedError

            def get_candles(self, request):
                raise NotImplementedError

        self.assertIsInstance(Dummy(), MarketDataSource)


if __name__ == "__main__":
    unittest.main()
