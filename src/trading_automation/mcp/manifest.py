"""Strict loader and verifier for the approved TradingView MCP manifest.

The manifest at ``config/mcp-tools.json`` records the exact tool catalog,
risk classification, and Phase 1 access decision for the pinned MCP source
commit.  It is a policy artifact, not a runtime cache: every field is checked
against the constants below so that editing the JSON cannot silently widen
access or reclassify a tool.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from trading_automation.mcp.errors import McpConfigurationError, McpPolicyError

MANIFEST_SCHEMA_VERSION = 1
EXPECTED_SERVER_NAME = "tradingview"
EXPECTED_SERVER_VERSION = "2.0.0"
EXPECTED_REPOSITORY = "https://github.com/Right-Bruno/tradingview-mcp.git"
EXPECTED_COMMIT = "5afdf7d0ee389909bc15dd1c7806796e3ae1b8e7"
EXPECTED_ENTRYPOINT = "src/server.js"
EXPECTED_TRANSPORT = "stdio"
EXPECTED_TOTAL = 84
EXPECTED_READ_ONLY = 31
EXPECTED_SIDE_EFFECTING = 53
EXPECTED_ALLOWED = frozenset({"tv_health_check", "chart_get_state", "data_get_ohlcv"})
EXPECTED_DENIED = 81
EXPECTED_CRITERION = (
    "Read-only means the tool only reads TradingView state or data. It may "
    "transiently open a fixed panel/editor for observation and, for "
    "capture_screenshot, write a local PNG. Side-effecting means the tool "
    "changes chart, study, drawing, alert, watchlist, layout, tab, script, "
    "or replay state; injects free-form input, clicks, keys, or arbitrary "
    "JavaScript; or launches/updates the MCP server."
)
EXPECTED_POLICY_REFERENCE = (
    "D-11: temporary tv-mcp-diagnostic agent with exactly three allowed tools"
)
MAX_MANIFEST_BYTES = 1_048_576
MAX_DESCRIPTION_LENGTH = 2_000
MAX_REASON_LENGTH = 2_000
_TOOL_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_TOOL_SOURCE_RE = re.compile(r"^src/tools/[A-Za-z0-9_.-]+\.js$")

DEFAULT_MANIFEST_RELATIVE_PATH = Path("config") / "mcp-tools.json"


@dataclass(frozen=True, slots=True)
class ManifestSource:
    server_name: str
    server_version: str
    repository: str
    commit: str
    entrypoint: str
    transport: str


@dataclass(frozen=True, slots=True)
class ManifestClassification:
    total: int
    read_only: int
    side_effecting: int
    criterion: str


@dataclass(frozen=True, slots=True)
class ManifestAccess:
    total: int
    allowed_count: int
    denied_count: int
    allowed: tuple[str, ...]
    policy_reference: str


@dataclass(frozen=True, slots=True)
class ToolEntry:
    name: str
    opencode_name: str
    description: str
    risk: str
    phase1_access: str
    source_file: str
    risk_reason: str

    @property
    def is_allowed(self) -> bool:
        return self.phase1_access == "allowed"

    @property
    def is_read_only(self) -> bool:
        return self.risk == "read_only"


@dataclass(frozen=True, slots=True)
class ToolManifest:
    schema_version: int
    source: ManifestSource
    classification: ManifestClassification
    access: ManifestAccess
    tools: tuple[ToolEntry, ...]

    @property
    def names(self) -> frozenset[str]:
        return frozenset(tool.name for tool in self.tools)

    @property
    def opencode_names(self) -> frozenset[str]:
        return frozenset(tool.opencode_name for tool in self.tools)

    @property
    def allowed_tools(self) -> tuple[ToolEntry, ...]:
        return tuple(tool for tool in self.tools if tool.is_allowed)

    @property
    def denied_tools(self) -> tuple[ToolEntry, ...]:
        return tuple(tool for tool in self.tools if not tool.is_allowed)

    def tool(self, name: str) -> ToolEntry:
        for tool in self.tools:
            if tool.name == name:
                return tool
        raise KeyError(name)


@dataclass(frozen=True, slots=True)
class ManifestExpectations:
    """Expected policy values; production uses the pinned defaults.

    Tests inject small fixtures with their own commit/counts.  Production
    callers must use :data:`DEFAULT_EXPECTATIONS`, which pins every value
    approved for Stage 2.
    """

    server_name: str = EXPECTED_SERVER_NAME
    server_version: str = EXPECTED_SERVER_VERSION
    repository: str = EXPECTED_REPOSITORY
    commit: str = EXPECTED_COMMIT
    entrypoint: str = EXPECTED_ENTRYPOINT
    transport: str = EXPECTED_TRANSPORT
    total: int = EXPECTED_TOTAL
    read_only: int = EXPECTED_READ_ONLY
    side_effecting: int = EXPECTED_SIDE_EFFECTING
    allowed: frozenset[str] = EXPECTED_ALLOWED
    denied: int = EXPECTED_DENIED
    criterion: str = EXPECTED_CRITERION
    policy_reference: str = EXPECTED_POLICY_REFERENCE


DEFAULT_EXPECTATIONS = ManifestExpectations()


def manifest_path(project_root: str | Path) -> Path:
    """Return the manifest path for a project root."""

    return Path(project_root) / DEFAULT_MANIFEST_RELATIVE_PATH


def load_manifest(
    path: str | Path,
    *,
    expectations: ManifestExpectations = DEFAULT_EXPECTATIONS,
) -> ToolManifest:
    """Load and fully validate the manifest.

    Raises :class:`McpConfigurationError` for missing/unreadable files and
    :class:`McpPolicyError` for any content that does not match the approved
    policy.
    """

    manifest_file = Path(path)
    try:
        size = manifest_file.stat().st_size
    except OSError as exc:
        raise McpConfigurationError(
            f"MCP manifest is not readable: {manifest_file}"
        ) from exc
    if size > MAX_MANIFEST_BYTES:
        raise McpConfigurationError(
            f"MCP manifest exceeds {MAX_MANIFEST_BYTES} bytes: {manifest_file}"
        )

    try:
        text = manifest_file.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise McpConfigurationError(
            f"MCP manifest is not readable: {manifest_file}"
        ) from exc

    try:
        raw = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except (json.JSONDecodeError, ValueError) as exc:
        raise McpPolicyError(f"MCP manifest is not valid JSON: {exc}") from exc

    document = _require_mapping(
        raw,
        "manifest",
        required={"schema_version", "source", "classification", "phase1_access", "tools"},
        optional=(),
    )
    schema_version = _require_int(document["schema_version"], "schema_version")
    if schema_version != MANIFEST_SCHEMA_VERSION:
        raise McpPolicyError(
            f"Unsupported MCP manifest schema_version {schema_version}; "
            f"expected {MANIFEST_SCHEMA_VERSION}"
        )

    source = _parse_source(document["source"], expectations)
    classification = _parse_classification(
        document["classification"], expectations
    )
    access = _parse_access(document["phase1_access"], expectations)
    tools = _parse_tools(document["tools"], expectations)

    _validate_tool_set(tools, classification, access, expectations)
    return ToolManifest(
        schema_version=schema_version,
        source=source,
        classification=classification,
        access=access,
        tools=tools,
    )


def verify_catalog(
    manifest: ToolManifest,
    catalog_names: Iterable[str],
    *,
    expectations: ManifestExpectations = DEFAULT_EXPECTATIONS,
) -> None:
    """Require the live catalog to match the manifest exactly."""

    names = list(catalog_names)
    if len(names) != len(set(names)):
        duplicates = sorted(_duplicates(names))
        raise McpPolicyError(
            "MCP catalog contains duplicate tool names: " + ", ".join(duplicates)
        )
    for name in names:
        if not isinstance(name, str) or not name:
            raise McpPolicyError("MCP catalog contains a non-string tool name")

    expected = manifest.names
    actual = set(names)
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    if missing or extra:
        parts: list[str] = []
        if missing:
            parts.append(f"missing={missing}")
        if extra:
            parts.append(f"extra={extra}")
        raise McpPolicyError(
            "MCP catalog does not match the pinned manifest ("
            + "; ".join(parts)
            + ")"
        )
    if len(names) != expectations.total:
        raise McpPolicyError(
            f"MCP catalog has {len(names)} tools; expected {expectations.total}"
        )


def _parse_source(
    raw: Any, expectations: ManifestExpectations
) -> ManifestSource:
    value = _require_mapping(
        raw,
        "source",
        required={
            "server_name",
            "server_version",
            "repository",
            "commit",
            "entrypoint",
            "transport",
        },
        optional=(),
    )
    source = ManifestSource(
        server_name=_require_str(value["server_name"], "source.server_name"),
        server_version=_require_str(
            value["server_version"], "source.server_version"
        ),
        repository=_require_str(value["repository"], "source.repository"),
        commit=_require_str(value["commit"], "source.commit"),
        entrypoint=_require_str(value["entrypoint"], "source.entrypoint"),
        transport=_require_str(value["transport"], "source.transport"),
    )
    checks = (
        ("server_name", source.server_name, expectations.server_name),
        ("server_version", source.server_version, expectations.server_version),
        ("repository", source.repository, expectations.repository),
        ("commit", source.commit, expectations.commit),
        ("entrypoint", source.entrypoint, expectations.entrypoint),
        ("transport", source.transport, expectations.transport),
    )
    for label, actual, expected in checks:
        if actual != expected:
            raise McpPolicyError(
                f"MCP manifest source.{label} is {actual!r}; expected {expected!r}"
            )
    if not _COMMIT_RE.match(source.commit):
        raise McpPolicyError(
            "MCP manifest source.commit must be a 40-character lowercase hex SHA"
        )
    return source


def _parse_classification(
    raw: Any, expectations: ManifestExpectations
) -> ManifestClassification:
    value = _require_mapping(
        raw,
        "classification",
        required={"total", "read_only", "side_effecting", "criterion"},
        optional=(),
    )
    classification = ManifestClassification(
        total=_require_int(value["total"], "classification.total"),
        read_only=_require_int(
            value["read_only"], "classification.read_only"
        ),
        side_effecting=_require_int(
            value["side_effecting"], "classification.side_effecting"
        ),
        criterion=_require_str(value["criterion"], "classification.criterion"),
    )
    expected = (
        ("total", classification.total, expectations.total),
        ("read_only", classification.read_only, expectations.read_only),
        ("side_effecting", classification.side_effecting, expectations.side_effecting),
    )
    for label, actual, wanted in expected:
        if actual != wanted:
            raise McpPolicyError(
                f"MCP manifest classification.{label} is {actual}; expected {wanted}"
            )
    if classification.criterion != expectations.criterion:
        raise McpPolicyError(
            "MCP manifest classification.criterion does not match the approved "
            "read-only/side-effecting criterion"
        )
    return classification


def _parse_access(
    raw: Any, expectations: ManifestExpectations
) -> ManifestAccess:
    value = _require_mapping(
        raw,
        "phase1_access",
        required={
            "total",
            "allowed_count",
            "denied_count",
            "allowed",
            "policy_reference",
        },
        optional=(),
    )
    allowed_raw = value["allowed"]
    if not isinstance(allowed_raw, list) or not all(
        isinstance(item, str) for item in allowed_raw
    ):
        raise McpPolicyError("MCP manifest phase1_access.allowed must be a list of strings")
    access = ManifestAccess(
        total=_require_int(value["total"], "phase1_access.total"),
        allowed_count=_require_int(
            value["allowed_count"], "phase1_access.allowed_count"
        ),
        denied_count=_require_int(
            value["denied_count"], "phase1_access.denied_count"
        ),
        allowed=tuple(allowed_raw),
        policy_reference=_require_str(
            value["policy_reference"], "phase1_access.policy_reference"
        ),
    )
    if access.total != expectations.total:
        raise McpPolicyError(
            f"MCP manifest phase1_access.total is {access.total}; "
            f"expected {expectations.total}"
        )
    if access.allowed_count != len(expectations.allowed):
        raise McpPolicyError(
            f"MCP manifest phase1_access.allowed_count is {access.allowed_count}; "
            f"expected {len(expectations.allowed)}"
        )
    if access.denied_count != expectations.denied:
        raise McpPolicyError(
            f"MCP manifest phase1_access.denied_count is {access.denied_count}; "
            f"expected {expectations.denied}"
        )
    if set(access.allowed) != expectations.allowed:
        raise McpPolicyError(
            "MCP manifest phase1_access.allowed does not match the approved "
            f"tool set: {sorted(access.allowed)}"
        )
    if len(access.allowed) != len(set(access.allowed)):
        raise McpPolicyError("MCP manifest phase1_access.allowed contains duplicates")
    if access.policy_reference != expectations.policy_reference:
        raise McpPolicyError(
            "MCP manifest phase1_access.policy_reference does not match decision D-11"
        )
    return access


def _parse_tools(
    raw: Any, expectations: ManifestExpectations
) -> tuple[ToolEntry, ...]:
    if not isinstance(raw, list):
        raise McpPolicyError("MCP manifest tools must be a list")
    if len(raw) != expectations.total:
        raise McpPolicyError(
            f"MCP manifest has {len(raw)} tools; expected {expectations.total}"
        )

    tools: list[ToolEntry] = []
    seen_names: set[str] = set()
    seen_opencode_names: set[str] = set()
    for index, item in enumerate(raw):
        label = f"tools[{index}]"
        value = _require_mapping(
            item,
            label,
            required={
                "name",
                "opencode_name",
                "description",
                "risk",
                "phase1_access",
                "source_file",
                "risk_reason",
            },
            optional=(),
        )
        name = _require_str(value["name"], f"{label}.name")
        opencode_name = _require_str(
            value["opencode_name"], f"{label}.opencode_name"
        )
        description = _require_str(
            value["description"], f"{label}.description"
        )
        risk = _require_str(value["risk"], f"{label}.risk")
        phase1_access = _require_str(
            value["phase1_access"], f"{label}.phase1_access"
        )
        source_file = _require_str(
            value["source_file"], f"{label}.source_file"
        )
        risk_reason = _require_str(
            value["risk_reason"], f"{label}.risk_reason"
        )

        if not _TOOL_NAME_RE.fullmatch(name):
            raise McpPolicyError(
                f"{label}.name {name!r} is not a lowercase MCP tool name"
            )
        if name in seen_names:
            raise McpPolicyError(f"{label}.name {name!r} is duplicated")
        seen_names.add(name)

        expected_opencode_name = f"tradingview_{name}"
        if opencode_name != expected_opencode_name:
            raise McpPolicyError(
                f"{label}.opencode_name is {opencode_name!r}; "
                f"expected {expected_opencode_name!r}"
            )
        if opencode_name in seen_opencode_names:
            raise McpPolicyError(
                f"{label}.opencode_name {opencode_name!r} is duplicated"
            )
        seen_opencode_names.add(opencode_name)

        if not description:
            raise McpPolicyError(f"{label}.description must not be empty")
        if len(description) > MAX_DESCRIPTION_LENGTH:
            raise McpPolicyError(
                f"{label}.description exceeds {MAX_DESCRIPTION_LENGTH} characters"
            )
        if risk not in {"read_only", "side_effecting"}:
            raise McpPolicyError(
                f"{label}.risk must be 'read_only' or 'side_effecting'"
            )
        if phase1_access not in {"allowed", "denied"}:
            raise McpPolicyError(
                f"{label}.phase1_access must be 'allowed' or 'denied'"
            )
        if not _TOOL_SOURCE_RE.fullmatch(source_file):
            raise McpPolicyError(
                f"{label}.source_file {source_file!r} must be a relative "
                "path under src/tools/ ending in .js"
            )
        _validate_relative_source_path(source_file, label)
        if not risk_reason:
            raise McpPolicyError(f"{label}.risk_reason must not be empty")
        if len(risk_reason) > MAX_REASON_LENGTH:
            raise McpPolicyError(
                f"{label}.risk_reason exceeds {MAX_REASON_LENGTH} characters"
            )

        if name in expectations.allowed:
            if phase1_access != "allowed":
                raise McpPolicyError(
                    f"{label} {name!r} is approved for Phase 1 but marked denied"
                )
            if risk != "read_only":
                raise McpPolicyError(
                    f"{label} {name!r} is approved for Phase 1 but classified "
                    f"as {risk!r}"
                )
        elif phase1_access != "denied":
            raise McpPolicyError(
                f"{label} {name!r} is not in the approved Phase 1 set but is "
                "marked allowed"
            )

        tools.append(
            ToolEntry(
                name=name,
                opencode_name=opencode_name,
                description=description,
                risk=risk,
                phase1_access=phase1_access,
                source_file=source_file,
                risk_reason=risk_reason,
            )
        )

    return tuple(tools)


def _validate_tool_set(
    tools: tuple[ToolEntry, ...],
    classification: ManifestClassification,
    access: ManifestAccess,
    expectations: ManifestExpectations,
) -> None:
    if len(tools) != expectations.total:
        raise McpPolicyError(
            f"MCP manifest has {len(tools)} tools; expected {expectations.total}"
        )

    read_only = sum(1 for tool in tools if tool.is_read_only)
    side_effecting = len(tools) - read_only
    if read_only != classification.read_only:
        raise McpPolicyError(
            f"MCP manifest declares {classification.read_only} read-only tools "
            f"but contains {read_only}"
        )
    if side_effecting != classification.side_effecting:
        raise McpPolicyError(
            f"MCP manifest declares {classification.side_effecting} "
            f"side-effecting tools but contains {side_effecting}"
        )
    if classification.total != read_only + side_effecting:
        raise McpPolicyError(
            "MCP manifest classification.total does not equal read_only + "
            "side_effecting"
        )

    allowed = {tool.name for tool in tools if tool.is_allowed}
    if allowed != set(access.allowed):
        raise McpPolicyError(
            "MCP manifest tool phase1_access values do not match "
            "phase1_access.allowed"
        )
    if len(allowed) != access.allowed_count:
        raise McpPolicyError(
            "MCP manifest phase1_access.allowed_count does not match tool entries"
        )
    denied = len(tools) - len(allowed)
    if denied != access.denied_count:
        raise McpPolicyError(
            "MCP manifest phase1_access.denied_count does not match tool entries"
        )
    if allowed != set(expectations.allowed):
        raise McpPolicyError(
            "MCP manifest Phase 1 tools do not match the approved set"
        )


def _validate_relative_source_path(source_file: str, label: str) -> None:
    path = PurePosixPath(source_file)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise McpPolicyError(
            f"{label}.source_file must be a normalized relative path"
        )


def _require_mapping(
    value: Any,
    label: str,
    *,
    required: set[str],
    optional: tuple[str, ...],
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise McpPolicyError(f"MCP manifest {label} must be an object")
    keys = set(value)
    missing = required - keys
    if missing:
        raise McpPolicyError(
            f"MCP manifest {label} is missing keys: {sorted(missing)}"
        )
    unknown = keys - required - set(optional)
    if unknown:
        raise McpPolicyError(
            f"MCP manifest {label} has unknown keys: {sorted(unknown)}"
        )
    return value


def _require_str(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise McpPolicyError(f"MCP manifest {label} must be a non-empty string")
    return value


def _require_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise McpPolicyError(f"MCP manifest {label} must be an integer")
    if value < 0:
        raise McpPolicyError(f"MCP manifest {label} must not be negative")
    return value


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key!r}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number is not allowed: {value}")


def _duplicates(values: Iterable[str]) -> set[str]:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for value in values:
        if value in seen:
            duplicates.add(value)
        seen.add(value)
    return duplicates
