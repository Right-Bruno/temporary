import tempfile
import unittest
from pathlib import Path

from trading_automation.data.market_data import (
    AmbiguousSourceError,
    InstrumentDefinition,
    InstrumentNotMappedError,
    InstrumentRef,
    SourceBinding,
    SymbolRegistry,
)

CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "instruments.toml"


def instrument(canonical_id="EURUSD", asset_class="forex"):
    return InstrumentRef(
        canonical_id=canonical_id,
        asset_class=asset_class,
        display_name=f"{canonical_id} display",
    )


def binding(
    *,
    adapter="tradingview",
    symbol="FOREXCOM:EURUSD",
    label="tradingview-mcp:FOREXCOM",
    status="CANDIDATE",
):
    return SourceBinding(
        adapter=adapter,
        source_symbol=symbol,
        source_label=label,
        status=status,
    )


def document(entries):
    return {"schema_version": 1, "instruments": entries}


def entry(canonical_id="EURUSD", **overrides):
    values = {
        "canonical_id": canonical_id,
        "asset_class": "forex",
        "display_name": f"{canonical_id} display",
    }
    values.update(overrides)
    return values


class ShippedInstrumentConfigTests(unittest.TestCase):
    def setUp(self):
        self.registry = SymbolRegistry.from_toml(CONFIG_PATH)

    def test_loads_the_phase_one_registry(self):
        self.assertEqual(
            self.registry.canonical_ids,
            ("EURUSD", "GBPUSD", "XAUUSD", "XAGUSD", "XCUUSD", "NASUSD", "SP500USD"),
        )

    def test_canonical_and_source_identity_stay_separate(self):
        definition = self.registry.get("EURUSD")
        self.assertEqual(definition.instrument.canonical_id, "EURUSD")
        self.assertEqual(len(definition.bindings), 1)
        binding = definition.bindings[0]
        self.assertEqual(binding.source_symbol, "FOREXCOM:EURUSD")
        self.assertEqual(binding.source_label, "tradingview-mcp:FOREXCOM")
        self.assertNotEqual(binding.source_symbol, definition.instrument.canonical_id)
        self.assertFalse(binding.is_verified)

    def test_no_candidate_is_marked_verified(self):
        for definition in self.registry:
            for binding in definition.bindings:
                self.assertEqual(binding.status, "CANDIDATE")

    def test_no_mt5_binding_exists(self):
        for definition in self.registry:
            for binding in definition.bindings:
                self.assertNotIn(
                    binding.adapter.lower(),
                    {"mt5", "metatrader", "metatrader5"},
                )

    def test_find_binding_uses_exact_provider_identity(self):
        binding = self.registry.find_binding(
            "EURUSD",
            adapter="tradingview",
            source_symbol="FOREXCOM:EURUSD",
            source_label="tradingview-mcp:FOREXCOM",
        )
        self.assertEqual(binding.status, "CANDIDATE")

    def test_unknown_binding_is_rejected(self):
        with self.assertRaises(InstrumentNotMappedError):
            self.registry.find_binding(
                "EURUSD",
                adapter="tradingview",
                source_label="unknown",
            )


class SymbolRegistryValidationTests(unittest.TestCase):
    def test_accepts_a_valid_in_memory_document(self):
        registry = SymbolRegistry.from_mapping(
            document(
                [
                    entry(
                        bindings=[
                            {
                                "adapter": "csv",
                                "source_symbol": "EURUSD",
                                "source_label": "TEST_FIXTURE",
                                "status": "CANDIDATE",
                            }
                        ]
                    )
                ]
            )
        )
        self.assertFalse(registry.get("EURUSD").bindings[0].is_verified)

    def test_rejects_duplicate_canonical_ids(self):
        with self.assertRaisesRegex(ValueError, "duplicate canonical"):
            SymbolRegistry.from_mapping(
                document([entry("EURUSD"), entry("EURUSD")])
            )

    def test_rejects_duplicate_bindings_across_instruments(self):
        duplicate = {
            "adapter": "tradingview",
            "source_symbol": "FOREXCOM:EURUSD",
            "source_label": "tradingview-mcp:FOREXCOM",
            "status": "CANDIDATE",
        }
        with self.assertRaisesRegex(ValueError, "registered more than once"):
            SymbolRegistry.from_mapping(
                document(
                    [
                        entry("EURUSD", bindings=[duplicate]),
                        entry("GBPUSD", bindings=[duplicate]),
                    ]
                )
            )

    def test_rejects_unsupported_asset_class(self):
        with self.assertRaisesRegex(ValueError, "asset_class"):
            SymbolRegistry.from_mapping(
                document([entry(asset_class="crypto")])
            )

    def test_rejects_unknown_keys_instead_of_ignoring_them(self):
        with self.assertRaisesRegex(ValueError, "unsupported key"):
            SymbolRegistry.from_mapping(
                {"schema_version": 1, "instruments": [], "extra": True}
            )
        with self.assertRaisesRegex(ValueError, "unsupported key"):
            SymbolRegistry.from_mapping(
                document([entry(verified=True)])
            )

    def test_rejects_mt5_mapping(self):
        with self.assertRaisesRegex(ValueError, "MT5"):
            SymbolRegistry.from_mapping(
                document(
                    [
                        entry(
                            bindings=[
                                {
                                    "adapter": "MT5",
                                    "source_symbol": "EURUSD.a",
                                    "source_label": "broker",
                                    "status": "CANDIDATE",
                                }
                            ]
                        )
                    ]
                )
            )

    def test_verified_binding_requires_explicit_evidence(self):
        with self.assertRaisesRegex(ValueError, "requires verified_at"):
            SymbolRegistry.from_mapping(
                document(
                    [
                        entry(
                            bindings=[
                                {
                                    "adapter": "tradingview",
                                    "source_symbol": "FOREXCOM:EURUSD",
                                    "source_label": "tradingview-mcp:FOREXCOM",
                                    "status": "VERIFIED",
                                }
                            ]
                        )
                    ]
                )
            )

    def test_verified_binding_with_evidence_is_accepted(self):
        registry = SymbolRegistry.from_mapping(
            document(
                [
                    entry(
                        bindings=[
                            {
                                "adapter": "tradingview",
                                "source_symbol": "FOREXCOM:EURUSD",
                                "source_label": "tradingview-mcp:FOREXCOM",
                                "status": "VERIFIED",
                                "verified_at": "2026-11-08T12:00:00+00:00",
                                "verification_reference": "owner-approved evidence",
                            }
                        ]
                    )
                ]
            )
        )
        self.assertTrue(registry.get("EURUSD").bindings[0].is_verified)

    def test_unverified_status_cannot_carry_verification_evidence(self):
        with self.assertRaisesRegex(ValueError, "only valid for a VERIFIED"):
            SymbolRegistry.from_mapping(
                document(
                    [
                        entry(
                            bindings=[
                                {
                                    "adapter": "tradingview",
                                    "source_symbol": "FOREXCOM:EURUSD",
                                    "source_label": "tradingview-mcp:FOREXCOM",
                                    "status": "CANDIDATE",
                                    "verified_at": "2026-11-08T12:00:00+00:00",
                                    "verification_reference": "not verified yet",
                                }
                            ]
                        )
                    ]
                )
            )

    def test_verified_at_must_be_timezone_aware(self):
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            SymbolRegistry.from_mapping(
                document(
                    [
                        entry(
                            bindings=[
                                {
                                    "adapter": "tradingview",
                                    "source_symbol": "FOREXCOM:EURUSD",
                                    "source_label": "tradingview-mcp:FOREXCOM",
                                    "status": "VERIFIED",
                                    "verified_at": "2026-11-08T12:00:00",
                                    "verification_reference": "owner-approved evidence",
                                }
                            ]
                        )
                    ]
                )
            )

    def test_rejects_invalid_canonical_id_pattern(self):
        with self.assertRaises(ValueError):
            SymbolRegistry.from_mapping(
                document([entry(canonical_id="eurusd")])
            )

    def test_rejects_empty_registry(self):
        with self.assertRaises(ValueError):
            SymbolRegistry([])

    def test_ambiguous_binding_lookup_requires_more_specificity(self):
        registry = SymbolRegistry(
            [
                InstrumentDefinition(
                    instrument=instrument(),
                    bindings=(
                        binding(symbol="FOREXCOM:EURUSD", label="feed-a"),
                        binding(symbol="FOREXCOM:EURUSD", label="feed-b"),
                    ),
                )
            ]
        )
        with self.assertRaises(AmbiguousSourceError):
            registry.find_binding("EURUSD", adapter="tradingview")
        resolved = registry.find_binding(
            "EURUSD",
            adapter="tradingview",
            source_label="feed-b",
        )
        self.assertEqual(resolved.source_label, "feed-b")

    def test_missing_default_config_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaises(FileNotFoundError):
                SymbolRegistry.load_default(temp_dir)


if __name__ == "__main__":
    unittest.main()
