"""IANA timezone conversion utilities for canonical New York timestamps.

STS V1.0 §2.2 requires ``America/New_York`` including daylight-saving
transitions. Candle timestamps represent candle opening times; the canonical
stored representation is New York local time with its UTC offset, while an
absolute UTC epoch value is used for identity and ordering.

No session-boundary inclusion/exclusion logic is implemented here.
"""

from __future__ import annotations

import importlib.util
import zoneinfo
from datetime import datetime, time, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

NEW_YORK_TIMEZONE_NAME = "America/New_York"

# STS V1.0 §2.2 and §2.4: these remain New York wall-clock definitions.
# They are reference values only; session-boundary inclusivity is still OPEN.
CHART_DAY_START = time(17, 0)
PERFORMANCE_DAY_START = time(0, 0)
EARLY_MORNING_SESSION = (time(2, 0), time(5, 0))
MORNING_SESSION = (time(7, 0), time(10, 30))
EVENING_SESSION = (time(13, 30), time(16, 0))

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


class InvalidTimezoneError(ValueError):
    """Raised when an IANA timezone name is not present in the timezone data."""


class TimezoneDataUnavailableError(RuntimeError):
    """Raised when no IANA timezone database can be loaded."""


class NaiveTimestampError(ValueError):
    """Raised when a timestamp has no offset and no source timezone."""


class AmbiguousLocalTimeError(ValueError):
    """Raised when a local wall-clock time occurs twice at a DST transition."""


class NonexistentLocalTimeError(ValueError):
    """Raised when a local wall-clock time is skipped by a DST transition."""


def _timezone_data_available() -> bool:
    """Return whether ``tzdata`` or a system IANA database is available."""
    spec = importlib.util.find_spec("tzdata")
    if spec is not None and spec.submodule_search_locations:
        package_dir = Path(next(iter(spec.submodule_search_locations)))
        if (package_dir / "zoneinfo").is_dir():
            return True
    return any(Path(path).is_dir() for path in zoneinfo.TZPATH)


@lru_cache(maxsize=None)
def load_timezone(name: str) -> ZoneInfo:
    """Load an IANA timezone by name, with explicit failure modes."""
    if not isinstance(name, str) or not name.strip():
        raise ValueError("timezone name must be a non-empty IANA name")
    normalised = name.strip()
    try:
        return ZoneInfo(normalised)
    except ZoneInfoNotFoundError as exc:
        if not _timezone_data_available():
            raise TimezoneDataUnavailableError(
                "IANA timezone data is unavailable; install the 'tzdata' package"
            ) from exc
        raise InvalidTimezoneError(f"Unknown IANA timezone: {normalised!r}") from exc


def new_york_timezone() -> ZoneInfo:
    """Return the canonical New York timezone."""
    return load_timezone(NEW_YORK_TIMEZONE_NAME)


def _coerce_timezone(source_timezone: str | ZoneInfo) -> ZoneInfo:
    if isinstance(source_timezone, ZoneInfo):
        return source_timezone
    return load_timezone(source_timezone)


def _attach_local_time(value: datetime, zone: ZoneInfo) -> datetime:
    """Attach a zone to a naive wall-clock time without guessing a DST fold."""
    first = value.replace(tzinfo=zone, fold=0)
    second = value.replace(tzinfo=zone, fold=1)
    candidates = (first, second)
    valid = [
        candidate
        for candidate in candidates
        if candidate.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None) == value
    ]

    if first.utcoffset() != second.utcoffset():
        if len(valid) == 2:
            raise AmbiguousLocalTimeError(
                f"Local timestamp {value.isoformat()} is ambiguous in {zone.key}; "
                "supply an offset-aware timestamp"
            )
        if not valid:
            raise NonexistentLocalTimeError(
                f"Local timestamp {value.isoformat()} does not exist in {zone.key}; "
                "supply an offset-aware timestamp"
            )
        return valid[0]

    if not valid:
        raise NonexistentLocalTimeError(
            f"Local timestamp {value.isoformat()} does not exist in {zone.key}; "
            "supply an offset-aware timestamp"
        )
    return candidates[0]


def ensure_aware(
    value: datetime,
    *,
    source_timezone: str | ZoneInfo | None = None,
) -> datetime:
    """Return an aware datetime, attaching an explicit source zone if needed.

    An aware timestamp's own offset is authoritative. ``source_timezone`` is
    used only when the input is naive, so an unknown source is never guessed.
    """
    if not isinstance(value, datetime):
        raise TypeError("timestamp must be a datetime")
    if value.tzinfo is not None and value.utcoffset() is not None:
        return value
    if source_timezone is None:
        raise NaiveTimestampError(
            "timestamp must be timezone-aware; naive timestamps are rejected "
            "unless an explicit source timezone is supplied"
        )
    return _attach_local_time(value, _coerce_timezone(source_timezone))


def to_new_york(
    value: datetime,
    *,
    source_timezone: str | ZoneInfo | None = None,
) -> datetime:
    """Convert an aware or explicitly zoned timestamp to America/New_York."""
    return ensure_aware(value, source_timezone=source_timezone).astimezone(new_york_timezone())


def to_utc_instant(
    value: datetime,
    *,
    source_timezone: str | ZoneInfo | None = None,
) -> datetime:
    """Convert an aware or explicitly zoned timestamp to an absolute UTC instant."""
    return ensure_aware(value, source_timezone=source_timezone).astimezone(timezone.utc)


def to_epoch_microseconds(
    value: datetime,
    *,
    source_timezone: str | ZoneInfo | None = None,
) -> int:
    """Return exact microseconds since the Unix epoch for identity and sorting."""
    utc_value = to_utc_instant(value, source_timezone=source_timezone)
    delta = utc_value - _EPOCH
    return delta.days * 86_400_000_000 + delta.seconds * 1_000_000 + delta.microseconds


def from_epoch_microseconds(value: int) -> datetime:
    """Return an aware UTC datetime from exact microseconds since the epoch."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("epoch microseconds must be an integer")
    days, remainder = divmod(value, 86_400_000_000)
    seconds, microseconds = divmod(remainder, 1_000_000)
    return _EPOCH + timedelta(days=days, seconds=seconds, microseconds=microseconds)


def serialize_new_york(
    value: datetime,
    *,
    source_timezone: str | ZoneInfo | None = None,
) -> str:
    """Serialize a timestamp as New York ISO 8601, retaining its UTC offset."""
    return to_new_york(value, source_timezone=source_timezone).isoformat()


def parse_iso8601(
    value: str,
    *,
    source_timezone: str | ZoneInfo | None = None,
) -> datetime:
    """Parse ISO 8601 and return the New York representation of the instant."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("timestamp must be a non-empty ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("timestamp must be ISO-8601") from exc
    return to_new_york(parsed, source_timezone=source_timezone)
