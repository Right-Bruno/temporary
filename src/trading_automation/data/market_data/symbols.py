"""Canonical instrument registry and source-binding resolution.

Canonical identity (:class:`InstrumentRef`) is deliberately separate from
provider-native identity (:class:`SourceBinding`).  A canonical id such as
``EURUSD`` never replaces the provider's ticker or the exact provenance label
stored on each candle.

Phase 1 does not map MT5 broker symbols.  Configuration is strict: unknown
keys, duplicate definitions, unsupported asset classes, and unverified
promotions are rejected rather than ignored.
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from trading_automation.data.market_data.contract import (
    AmbiguousSourceError,
    InstrumentNotMappedError,
    InstrumentRef,
    SourceBinding,
)

ALLOWED_ASSET_CLASSES = frozenset({"forex", "metals", "indices"})
_MT5_ADAPTER_NAMES = frozenset({"mt5", "metatrader", "metatrader5"})
_TOP_LEVEL_KEYS = frozenset({"schema_version", "instruments"})
_INSTRUMENT_KEYS = frozenset(
    {"canonical_id", "asset_class", "display_name", "bindings"}
)
_BINDING_KEYS = frozenset(
    {
        "adapter",
        "source_symbol",
        "source_label",
        "status",
        "verified_at",
        "verification_reference",
    }
)
_CANONICAL_ID_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9_.-]*$")


def _require_mapping(value: object, description: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{description} must be a TOML table")
    return value


def _reject_unknown_keys(
    value: dict[str, Any],
    allowed: frozenset[str],
    description: str,
) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ValueError(
            f"{description} has unsupported key(s): {', '.join(unknown)}"
        )


def _require_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    if value != value.strip():
        raise ValueError(f"{field_name} must not have leading or trailing whitespace")
    return value


def _parse_verified_at(value: object, description: str) -> datetime:
    text = _require_text(value, f"{description}.verified_at")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{description}.verified_at must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{description}.verified_at must be timezone-aware")
    return parsed


def _reject_mt5(adapter: str, description: str) -> None:
    if adapter.strip().lower() in _MT5_ADAPTER_NAMES:
        raise ValueError(
            f"{description}: MT5 broker-symbol mapping is not authorised in Phase 1"
        )


@dataclass(frozen=True, slots=True)
class InstrumentDefinition:
    """A canonical instrument plus its provider bindings."""

    instrument: InstrumentRef
    bindings: tuple[SourceBinding, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.instrument, InstrumentRef):
            raise TypeError("instrument must be an InstrumentRef")
        bindings = tuple(self.bindings)
        if any(not isinstance(binding, SourceBinding) for binding in bindings):
            raise TypeError("bindings must contain SourceBinding instances")

        keys: set[tuple[str, str, str]] = set()
        for binding in bindings:
            _reject_mt5(binding.adapter, f"binding {binding.source_label!r}")
            key = (binding.adapter, binding.source_symbol, binding.source_label)
            if key in keys:
                raise ValueError(
                    "duplicate binding for canonical instrument "
                    f"{self.instrument.canonical_id!r}: {key!r}"
                )
            keys.add(key)
        object.__setattr__(self, "bindings", bindings)


class SymbolRegistry:
    """Validated canonical-instrument and source-binding registry."""

    def __init__(self, definitions: Sequence[InstrumentDefinition]) -> None:
        values = tuple(definitions)
        if not values:
            raise ValueError("instrument registry must contain at least one instrument")
        if any(not isinstance(item, InstrumentDefinition) for item in values):
            raise TypeError("definitions must contain InstrumentDefinition instances")

        by_id: dict[str, InstrumentDefinition] = {}
        binding_keys: set[tuple[str, str, str]] = set()
        for definition in values:
            canonical_id = definition.instrument.canonical_id
            if canonical_id in by_id:
                raise ValueError(
                    f"duplicate canonical instrument id: {canonical_id!r}"
                )
            if definition.instrument.asset_class.lower() not in ALLOWED_ASSET_CLASSES:
                raise ValueError(
                    f"instrument {canonical_id!r} has unsupported asset_class "
                    f"{definition.instrument.asset_class!r}"
                )
            if definition.instrument.asset_class != definition.instrument.asset_class.lower():
                raise ValueError(
                    f"instrument {canonical_id!r} asset_class must be lowercase"
                )
            for binding in definition.bindings:
                key = (
                    binding.adapter,
                    binding.source_symbol,
                    binding.source_label,
                )
                if key in binding_keys:
                    raise ValueError(
                        "source binding is registered more than once: "
                        f"(adapter={key[0]!r}, source_symbol={key[1]!r}, "
                        f"source_label={key[2]!r})"
                    )
                binding_keys.add(key)
            by_id[canonical_id] = definition

        self._definitions = values
        self._by_id = by_id

    @property
    def definitions(self) -> tuple[InstrumentDefinition, ...]:
        return self._definitions

    @property
    def canonical_ids(self) -> tuple[str, ...]:
        return tuple(item.instrument.canonical_id for item in self._definitions)

    def __len__(self) -> int:
        return len(self._definitions)

    def __iter__(self) -> Iterator[InstrumentDefinition]:
        return iter(self._definitions)

    def __contains__(self, canonical_id: object) -> bool:
        return isinstance(canonical_id, str) and canonical_id in self._by_id

    def get(self, canonical_id: str) -> InstrumentDefinition:
        if not isinstance(canonical_id, str) or not canonical_id:
            raise TypeError("canonical_id must be a non-empty string")
        try:
            return self._by_id[canonical_id]
        except KeyError as exc:
            raise InstrumentNotMappedError(
                f"canonical instrument {canonical_id!r} is not registered",
                code="INSTRUMENT_NOT_MAPPED",
            ) from exc

    def bindings_for(self, canonical_id: str) -> tuple[SourceBinding, ...]:
        return self.get(canonical_id).bindings

    def find_binding(
        self,
        canonical_id: str,
        *,
        adapter: str,
        source_symbol: str | None = None,
        source_label: str | None = None,
    ) -> SourceBinding:
        """Return the single binding matching the supplied exact identifiers."""
        if not isinstance(adapter, str) or not adapter.strip():
            raise TypeError("adapter must be a non-empty string")
        matches = [
            binding
            for binding in self.bindings_for(canonical_id)
            if binding.adapter == adapter
            and (source_symbol is None or binding.source_symbol == source_symbol)
            and (source_label is None or binding.source_label == source_label)
        ]
        if not matches:
            raise InstrumentNotMappedError(
                f"no binding for {canonical_id!r} matches adapter={adapter!r}, "
                f"source_symbol={source_symbol!r}, source_label={source_label!r}",
                code="INSTRUMENT_NOT_MAPPED",
            )
        if len(matches) > 1:
            raise AmbiguousSourceError(
                f"{len(matches)} bindings for {canonical_id!r} match "
                f"adapter={adapter!r}; supply source_symbol and source_label",
                code="AMBIGUOUS_SOURCE",
            )
        return matches[0]

    @classmethod
    def from_toml(cls, path: str | Path) -> SymbolRegistry:
        config_path = Path(path)
        with config_path.open("rb") as handle:
            raw: object = tomllib.load(handle)
        return cls.from_mapping(raw)

    @classmethod
    def from_mapping(cls, raw: object) -> SymbolRegistry:
        """Build a registry from already-parsed TOML data (strict schema)."""
        document = _require_mapping(raw, "instruments configuration")
        _reject_unknown_keys(
            document,
            _TOP_LEVEL_KEYS,
            "instruments configuration",
        )
        schema_version = document.get("schema_version")
        if (
            not isinstance(schema_version, int)
            or isinstance(schema_version, bool)
            or schema_version != 1
        ):
            raise ValueError("schema_version must be the integer 1")

        entries = document.get("instruments")
        if not isinstance(entries, list) or not entries:
            raise ValueError("instruments must be a non-empty array of tables")

        definitions: list[InstrumentDefinition] = []
        for index, entry in enumerate(entries):
            description = f"instruments[{index}]"
            table = _require_mapping(entry, description)
            _reject_unknown_keys(table, _INSTRUMENT_KEYS, description)
            canonical_id = _require_text(
                table.get("canonical_id"),
                f"{description}.canonical_id",
            )
            if not _CANONICAL_ID_PATTERN.match(canonical_id):
                raise ValueError(
                    f"{description}.canonical_id must use uppercase letters, "
                    "digits, '_', '.', or '-' and must start with a letter or digit"
                )
            asset_class = _require_text(
                table.get("asset_class"),
                f"{description}.asset_class",
            ).lower()
            if asset_class not in ALLOWED_ASSET_CLASSES:
                allowed = ", ".join(sorted(ALLOWED_ASSET_CLASSES))
                raise ValueError(
                    f"{description}.asset_class must be one of: {allowed}"
                )
            display_name = _require_text(
                table.get("display_name"),
                f"{description}.display_name",
            )
            instrument = InstrumentRef(
                canonical_id=canonical_id,
                asset_class=asset_class,
                display_name=display_name,
            )

            raw_bindings = table.get("bindings", [])
            if not isinstance(raw_bindings, list):
                raise ValueError(f"{description}.bindings must be an array of tables")
            bindings = tuple(
                _parse_binding(item, f"{description}.bindings[{binding_index}]")
                for binding_index, item in enumerate(raw_bindings)
            )
            definitions.append(
                InstrumentDefinition(instrument=instrument, bindings=bindings)
            )

        return cls(definitions)

    @classmethod
    def load_default(cls, root: str | Path = ".") -> SymbolRegistry:
        """Load ``config/instruments.toml`` relative to the project root."""
        path = Path(root) / "config" / "instruments.toml"
        if not path.is_file():
            raise FileNotFoundError(
                f"instrument configuration not found: {path.name}"
            )
        return cls.from_toml(path)


def _parse_binding(raw: object, description: str) -> SourceBinding:
    table = _require_mapping(raw, description)
    _reject_unknown_keys(table, _BINDING_KEYS, description)

    adapter = _require_text(table.get("adapter"), f"{description}.adapter")
    _reject_mt5(adapter, description)
    status = _require_text(table.get("status"), f"{description}.status").upper()

    has_verified_at = "verified_at" in table
    has_verification_reference = "verification_reference" in table
    if status == "VERIFIED":
        if not has_verified_at or not has_verification_reference:
            raise ValueError(
                f"{description}: a VERIFIED binding requires verified_at and "
                "verification_reference evidence; candidate symbols must not be "
                "promoted silently"
            )
        _parse_verified_at(table.get("verified_at"), description)
        _require_text(
            table.get("verification_reference"),
            f"{description}.verification_reference",
        )
    elif has_verified_at or has_verification_reference:
        raise ValueError(
            f"{description}: verification evidence is only valid for a "
            "VERIFIED binding"
        )

    return SourceBinding(
        adapter=adapter,
        source_symbol=_require_text(
            table.get("source_symbol"),
            f"{description}.source_symbol",
        ),
        source_label=_require_text(
            table.get("source_label"),
            f"{description}.source_label",
        ),
        status=status,
    )
