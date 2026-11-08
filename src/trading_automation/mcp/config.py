"""OpenCode V2 project configuration and permission-policy verification.

Stage 2 uses the project-level ``opencode.jsonc`` to start the pinned local
TradingView MCP server.  The server is configured with ``codemode: false`` so
native tool names survive, and a top-level ``tradingview_*`` deny rule keeps
every tool unavailable unless a narrower agent rule appends an allow.

The verifier is deliberately strict: unknown fields, relative working
directories, non-loopback CDP hosts, extra environment variables, and any
policy rule that would widen TradingView access all fail closed.
"""

from __future__ import annotations

import fnmatch
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType
from typing import Any

from trading_automation.mcp.errors import McpConfigurationError, McpPolicyError
from trading_automation.mcp.manifest import (
    DEFAULT_EXPECTATIONS,
    ManifestExpectations,
    ToolManifest,
)

OPENCODE_SCHEMA_URL = "https://opencode.ai/config.json"
SERVER_NAME = "tradingview"
SERVER_COMMAND = ("node", "src/server.js")
CDP_HOST = "127.0.0.1"
CDP_PORT = "9222"
EXPECTED_TIMEOUTS = MappingProxyType(
    {"startup": 30_000, "catalog": 30_000, "execution": 60_000}
)
MAX_CONFIG_BYTES = 1_048_576
MAX_AGENT_BYTES = 65_536
AGENT_NAME = "tv-mcp-diagnostic"
AGENT_PATH = Path(".opencode") / "agents" / f"{AGENT_NAME}.md"

TOP_LEVEL_KEYS = frozenset({"$schema", "mcp", "permissions"})
MCP_KEYS = frozenset({"servers", "timeout"})
SERVER_KEYS = frozenset(
    {
        "type",
        "command",
        "cwd",
        "environment",
        "codemode",
        "timeout",
        "disabled",
        "protocol",
        "pool",
    }
)
AGENT_FRONTMATTER_KEYS = frozenset({"description", "mode", "permissions"})
PERMISSION_KEYS = frozenset({"action", "resource", "effect"})
EFFECTS = frozenset({"allow", "deny", "ask"})

@dataclass(frozen=True, slots=True)
class PermissionRule:
    action: str
    resource: str
    effect: str


# OpenCode's documented base policy, included so simulations model the real
# last-match-wins resolution rather than only the project rules.
BASE_POLICY = (
    PermissionRule(action="*", resource="*", effect="allow"),
    PermissionRule(action="read", resource="*.env", effect="ask"),
    PermissionRule(action="read", resource="*.env.*", effect="ask"),
    PermissionRule(action="read", resource="*.env.example", effect="allow"),
)

TOP_LEVEL_POLICY = (
    PermissionRule(action="tradingview_*", resource="*", effect="deny"),
    PermissionRule(
        action="opencode_list_mcp_resources",
        resource=SERVER_NAME,
        effect="deny",
    ),
    PermissionRule(
        action="opencode_read_mcp_resource",
        resource=f"{SERVER_NAME}:*",
        effect="deny",
    ),
)


@dataclass(frozen=True, slots=True)
class McpServerConfig:
    name: str
    type: str
    command: tuple[str, ...]
    cwd: Path
    environment: Mapping[str, str]
    codemode: bool
    timeouts: Mapping[str, int]
    protocol: str | None
    pool: bool | None
    disabled: bool


@dataclass(frozen=True, slots=True)
class OpenCodeConfig:
    path: Path
    server: McpServerConfig
    permissions: tuple[PermissionRule, ...]


@dataclass(frozen=True, slots=True)
class AgentConfig:
    name: str
    path: Path
    description: str
    mode: str
    permissions: tuple[PermissionRule, ...]
    instructions: str


def opencode_config_path(project_root: str | Path) -> Path:
    return Path(project_root) / "opencode.jsonc"


def agent_config_path(project_root: str | Path) -> Path:
    return Path(project_root) / AGENT_PATH


def load_jsonc(path: str | Path, *, max_bytes: int = MAX_CONFIG_BYTES) -> Any:
    """Load a JSONC document without third-party dependencies.

    Comments are replaced with whitespace (newlines preserved) and trailing
    commas are removed outside strings.  Duplicate keys and non-finite numbers
    are rejected.
    """

    file_path = Path(path)
    try:
        size = file_path.stat().st_size
    except OSError as exc:
        raise McpConfigurationError(f"Configuration is not readable: {file_path}") from exc
    if size > max_bytes:
        raise McpConfigurationError(
            f"Configuration exceeds {max_bytes} bytes: {file_path}"
        )
    try:
        text = file_path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise McpConfigurationError(f"Configuration is not readable: {file_path}") from exc

    try:
        stripped = _strip_jsonc_comments(text)
        without_trailing_commas = _strip_trailing_commas(stripped)
        return json.loads(
            without_trailing_commas,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except (json.JSONDecodeError, ValueError) as exc:
        raise McpConfigurationError(
            f"Configuration is not valid JSONC: {file_path}: {exc}"
        ) from exc


def load_opencode_config(project_root: str | Path) -> OpenCodeConfig:
    root = Path(project_root)
    path = opencode_config_path(root)
    raw = load_jsonc(path)
    document = _require_mapping(raw, "opencode config", allowed=TOP_LEVEL_KEYS)

    schema = document.get("$schema")
    if schema != OPENCODE_SCHEMA_URL:
        raise McpPolicyError(
            f"opencode.jsonc $schema must be {OPENCODE_SCHEMA_URL!r}"
        )

    mcp = _require_mapping(
        document.get("mcp"),
        "opencode mcp",
        allowed=MCP_KEYS,
        required={"servers", "timeout"},
    )
    timeouts = _parse_timeouts(mcp["timeout"])
    servers = _require_mapping(
        mcp["servers"], "opencode mcp.servers", allowed=frozenset({SERVER_NAME})
    )
    if set(servers) != {SERVER_NAME}:
        raise McpPolicyError(
            f"opencode.jsonc must define exactly one MCP server named {SERVER_NAME!r}"
        )
    server = _parse_server(servers[SERVER_NAME])

    rules_raw = document.get("permissions")
    if not isinstance(rules_raw, list):
        raise McpPolicyError("opencode.jsonc permissions must be a list")
    permissions = tuple(_parse_rule(item, f"permissions[{index}]") for index, item in enumerate(rules_raw))
    if permissions != TOP_LEVEL_POLICY:
        raise McpPolicyError(
            "opencode.jsonc top-level permissions must be exactly the approved "
            "TradingView deny rules"
        )

    return OpenCodeConfig(
        path=path,
        server=replace(server, timeouts=timeouts),
        permissions=permissions,
    )


def load_agent_config(
    project_root: str | Path,
    *,
    name: str = AGENT_NAME,
    expected_allowed: frozenset[str] = DEFAULT_EXPECTATIONS.allowed,
) -> AgentConfig:
    path = agent_config_path(project_root)
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise McpConfigurationError(f"Agent configuration is not readable: {path}") from exc
    if size > MAX_AGENT_BYTES:
        raise McpConfigurationError(
            f"Agent configuration exceeds {MAX_AGENT_BYTES} bytes: {path}"
        )
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise McpConfigurationError(f"Agent configuration is not readable: {path}") from exc

    frontmatter, body = _split_frontmatter(text)
    fields, permissions = _parse_agent_frontmatter(frontmatter)
    description = _require_nonempty_str(fields.get("description"), "agent description")
    mode = _require_nonempty_str(fields.get("mode"), "agent mode")
    if mode != "subagent":
        raise McpPolicyError("tv-mcp-diagnostic mode must be 'subagent'")
    if not body.strip():
        raise McpPolicyError("tv-mcp-diagnostic must contain instructions")

    agent = AgentConfig(
        name=name,
        path=path,
        description=description,
        mode=mode,
        permissions=permissions,
        instructions=body,
    )
    _validate_agent_rules(agent, expected_allowed)
    return agent


def verify_policy(
    config: OpenCodeConfig,
    agent: AgentConfig,
    manifest: ToolManifest,
    *,
    expectations: ManifestExpectations = DEFAULT_EXPECTATIONS,
) -> None:
    """Verify deny-by-default for every agent and exactly three agent allows."""

    if set(config.server.environment) != {"TV_CDP_HOST", "TV_CDP_PORT"}:
        raise McpPolicyError(
            "TradingView MCP environment must contain exactly TV_CDP_HOST and "
            "TV_CDP_PORT"
        )
    if config.server.environment["TV_CDP_HOST"] != CDP_HOST:
        raise McpPolicyError(
            f"TV_CDP_HOST must be {CDP_HOST!r}"
        )
    if config.server.environment["TV_CDP_PORT"] != CDP_PORT:
        raise McpPolicyError(f"TV_CDP_PORT must be {CDP_PORT!r}")

    for tool in manifest.tools:
        top_effect = resolve_effect(
            BASE_POLICY + config.permissions, tool.opencode_name, "*"
        )
        if top_effect != "deny":
            raise McpPolicyError(
                f"Top-level policy does not deny {tool.opencode_name} "
                f"(resolved effect: {top_effect})"
            )

        agent_effect = resolve_effect(
            BASE_POLICY + config.permissions + agent.permissions,
            tool.opencode_name,
            "*",
        )
        expected = "allow" if tool.name in expectations.allowed else "deny"
        if agent_effect != expected:
            raise McpPolicyError(
                f"Agent {agent.name} resolves {tool.opencode_name} to "
                f"{agent_effect}; expected {expected}"
            )

    for action, resource in (
        ("opencode_list_mcp_resources", SERVER_NAME),
        ("opencode_read_mcp_resource", f"{SERVER_NAME}:anything"),
    ):
        top_effect = resolve_effect(
            BASE_POLICY + config.permissions, action, resource
        )
        if top_effect != "deny":
            raise McpPolicyError(
                f"Top-level policy does not deny MCP resource access "
                f"{action}/{resource}"
            )
        agent_effect = resolve_effect(
            BASE_POLICY + config.permissions + agent.permissions,
            action,
            resource,
        )
        if agent_effect != "deny":
            raise McpPolicyError(
                f"Agent {agent.name} can access MCP resources "
                f"{action}/{resource}"
            )


def resolve_effect(
    rules: tuple[PermissionRule, ...] | list[PermissionRule],
    action: str,
    resource: str,
) -> str:
    """Resolve OpenCode's last-match-wins permission model."""

    effect = "ask"
    for rule in rules:
        if _matches(rule.action, action) and _matches(rule.resource, resource):
            effect = rule.effect
    return effect


def _matches(pattern: str, value: str) -> bool:
    # fnmatchcase's signature is (name, pattern), so the resource is the name.
    if os.name == "nt":
        return fnmatch.fnmatchcase(value.lower(), pattern.lower())
    return fnmatch.fnmatchcase(value, pattern)


def _parse_timeouts(raw: Any) -> Mapping[str, int]:
    value = _require_mapping(
        raw,
        "opencode mcp.timeout",
        allowed=frozenset(EXPECTED_TIMEOUTS),
        required=set(EXPECTED_TIMEOUTS),
    )
    result: dict[str, int] = {}
    for key, expected in EXPECTED_TIMEOUTS.items():
        actual = _require_int(value[key], f"mcp.timeout.{key}")
        if actual != expected:
            raise McpPolicyError(
                f"opencode mcp.timeout.{key} is {actual}; expected {expected}"
            )
        result[key] = actual
    return MappingProxyType(result)


def _parse_server(raw: Any) -> McpServerConfig:
    value = _require_mapping(
        raw,
        "opencode mcp.servers.tradingview",
        allowed=SERVER_KEYS,
        required={"type", "command", "cwd", "environment", "codemode"},
    )
    if value.get("type") != "local":
        raise McpPolicyError("TradingView MCP server type must be 'local'")
    if value.get("disabled", False) is not False:
        raise McpPolicyError("TradingView MCP server must not be disabled")
    if value.get("codemode") is not False:
        raise McpPolicyError(
            "TradingView MCP server codemode must be false so native tool names "
            "map one-to-one to permission actions"
        )

    command_raw = value["command"]
    if (
        not isinstance(command_raw, list)
        or not all(isinstance(part, str) and part for part in command_raw)
        or tuple(command_raw) != SERVER_COMMAND
    ):
        raise McpPolicyError(
            "TradingView MCP command must be exactly ['node', 'src/server.js']"
        )

    cwd_raw = value["cwd"]
    if not isinstance(cwd_raw, str) or not cwd_raw:
        raise McpPolicyError("TradingView MCP cwd must be a non-empty string")
    if not Path(cwd_raw).is_absolute():
        raise McpPolicyError("TradingView MCP cwd must be an absolute path")
    cwd = Path(cwd_raw)
    if not cwd.is_dir():
        raise McpPolicyError(
            f"TradingView MCP cwd does not exist or is not a directory: {cwd}"
        )

    environment_raw = value["environment"]
    if not isinstance(environment_raw, Mapping):
        raise McpPolicyError("TradingView MCP environment must be an object")
    environment: dict[str, str] = {}
    for key, item in environment_raw.items():
        if not isinstance(key, str) or not isinstance(item, str):
            raise McpPolicyError("TradingView MCP environment entries must be strings")
        environment[key] = item
    if set(environment) != {"TV_CDP_HOST", "TV_CDP_PORT"}:
        raise McpPolicyError(
            "TradingView MCP environment must contain exactly TV_CDP_HOST and "
            "TV_CDP_PORT"
        )
    if environment["TV_CDP_HOST"] != CDP_HOST:
        raise McpPolicyError(
            f"TV_CDP_HOST must be {CDP_HOST!r}; found "
            f"{environment['TV_CDP_HOST']!r}"
        )
    if environment["TV_CDP_PORT"] != CDP_PORT:
        raise McpPolicyError(
            f"TV_CDP_PORT must be {CDP_PORT!r}; found "
            f"{environment['TV_CDP_PORT']!r}"
        )

    protocol = value.get("protocol")
    if protocol not in (None, "legacy"):
        raise McpPolicyError(
            "TradingView MCP protocol must be absent or 'legacy'"
        )
    pool = value.get("pool")
    if pool not in (None, False):
        raise McpPolicyError("TradingView MCP pool must be absent or false")
    if "timeout" in value:
        raise McpPolicyError(
            "TradingView MCP server must use the approved mcp.timeout defaults"
        )

    return McpServerConfig(
        name=SERVER_NAME,
        type="local",
        command=SERVER_COMMAND,
        cwd=cwd,
        environment=MappingProxyType(environment),
        codemode=False,
        timeouts=MappingProxyType({}),
        protocol=protocol,
        pool=pool,
        disabled=False,
    )


def _parse_rule(raw: Any, label: str) -> PermissionRule:
    value = _require_mapping(
        raw, label, allowed=PERMISSION_KEYS, required=PERMISSION_KEYS
    )
    action = _require_nonempty_str(value["action"], f"{label}.action")
    resource = _require_nonempty_str(value["resource"], f"{label}.resource")
    effect = _require_nonempty_str(value["effect"], f"{label}.effect")
    if effect not in EFFECTS:
        raise McpPolicyError(
            f"{label}.effect must be one of {sorted(EFFECTS)}"
        )
    return PermissionRule(action=action, resource=resource, effect=effect)


def _split_frontmatter(text: str) -> tuple[str, str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise McpPolicyError("Agent file must start with YAML frontmatter")
    closing = None
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            closing = index
            break
    if closing is None:
        raise McpPolicyError("Agent frontmatter is not terminated by '---'")
    frontmatter = "\n".join(lines[1:closing])
    body = "\n".join(lines[closing + 1 :])
    return frontmatter, body


def _parse_agent_frontmatter(
    frontmatter: str,
) -> tuple[dict[str, str], tuple[PermissionRule, ...]]:
    fields: dict[str, str] = {}
    permissions: list[PermissionRule] = []
    current: dict[str, str] | None = None
    in_permissions = False

    for line_number, line in enumerate(frontmatter.splitlines(), start=2):
        if "\t" in line:
            raise McpPolicyError(
                f"Agent frontmatter line {line_number} must not contain tabs"
            )
        stripped = line.strip()
        if not stripped:
            continue
        if not in_permissions:
            if line.startswith((" ", "-")):
                raise McpPolicyError(
                    f"Agent frontmatter line {line_number} has unexpected indentation"
                )
            key, separator, value = line.partition(":")
            if not separator:
                raise McpPolicyError(
                    f"Agent frontmatter line {line_number} must be 'key: value'"
                )
            key = key.strip()
            if key not in AGENT_FRONTMATTER_KEYS:
                raise McpPolicyError(
                    f"Agent frontmatter has unknown key {key!r}"
                )
            if key in fields:
                raise McpPolicyError(f"Agent frontmatter key {key!r} is duplicated")
            if key == "permissions":
                if value.strip():
                    raise McpPolicyError(
                        "Agent permissions must use block list syntax"
                    )
                in_permissions = True
                continue
            fields[key] = _parse_yaml_scalar(value.strip(), key)
            continue

        if not line.startswith(" "):
            raise McpPolicyError(
                f"Agent permissions line {line_number} must be indented"
            )
        if stripped.startswith("- "):
            if current is not None:
                permissions.append(_finish_rule(current, line_number))
            current = {}
            key, separator, value = stripped[2:].partition(":")
            if not separator:
                raise McpPolicyError(
                    f"Agent permissions line {line_number} must be 'key: value'"
                )
            _set_rule_field(current, key.strip(), value.strip(), line_number)
            continue
        if current is None:
            raise McpPolicyError(
                f"Agent permissions line {line_number} is not inside a list item"
            )
        key, separator, value = stripped.partition(":")
        if not separator:
            raise McpPolicyError(
                f"Agent permissions line {line_number} must be 'key: value'"
            )
        _set_rule_field(current, key.strip(), value.strip(), line_number)

    if in_permissions and current is not None:
        permissions.append(_finish_rule(current, line_number=None))
    if not in_permissions:
        raise McpPolicyError("Agent frontmatter must define permissions")
    if not permissions:
        raise McpPolicyError("Agent permissions must not be empty")
    if "permissions" in fields:
        raise McpPolicyError("Agent frontmatter key 'permissions' is duplicated")
    return fields, tuple(permissions)


def _set_rule_field(
    current: dict[str, str], key: str, value: str, line_number: int
) -> None:
    if key not in PERMISSION_KEYS:
        raise McpPolicyError(
            f"Agent permission line {line_number} has unknown key {key!r}"
        )
    if key in current:
        raise McpPolicyError(
            f"Agent permission line {line_number} duplicates {key!r}"
        )
    current[key] = _parse_yaml_scalar(value, key)


def _finish_rule(current: dict[str, str], line_number: int | None) -> PermissionRule:
    missing = PERMISSION_KEYS - set(current)
    if missing:
        where = f" ending near line {line_number}" if line_number else ""
        raise McpPolicyError(
            f"Agent permission rule{where} is missing keys: {sorted(missing)}"
        )
    effect = current["effect"]
    if effect not in EFFECTS:
        raise McpPolicyError(
            f"Agent permission effect must be one of {sorted(EFFECTS)}"
        )
    return PermissionRule(
        action=current["action"],
        resource=current["resource"],
        effect=effect,
    )


def _parse_yaml_scalar(value: str, label: str) -> str:
    if not value:
        raise McpPolicyError(f"Agent frontmatter {label} must not be empty")
    if value.startswith('"'):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise McpPolicyError(
                f"Agent frontmatter {label} has invalid quoted value"
            ) from exc
        if not isinstance(parsed, str):
            raise McpPolicyError(f"Agent frontmatter {label} must be a string")
        return parsed
    if value.startswith("'"):
        if len(value) < 2 or not value.endswith("'"):
            raise McpPolicyError(
                f"Agent frontmatter {label} has an unterminated quoted value"
            )
        return value[1:-1].replace("''", "'")
    return value


def _validate_agent_rules(
    agent: AgentConfig, expected_allowed: frozenset[str]
) -> None:
    rules = agent.permissions
    if not rules:
        raise McpPolicyError("tv-mcp-diagnostic permissions must not be empty")
    if rules[0] != PermissionRule(action="*", resource="*", effect="deny"):
        raise McpPolicyError(
            "tv-mcp-diagnostic must start with a deny-all rule so its only "
            "access is the three approved TradingView tools"
        )

    expected_actions = {f"{SERVER_NAME}_{name}" for name in expected_allowed}
    allow_rules = [rule for rule in rules if rule.effect == "allow"]
    if len(allow_rules) != len(expected_allowed):
        raise McpPolicyError(
            f"tv-mcp-diagnostic must have exactly {len(expected_allowed)} allow "
            f"rules; found {len(allow_rules)}"
        )
    allow_actions = {rule.action for rule in allow_rules}
    if allow_actions != expected_actions:
        raise McpPolicyError(
            "tv-mcp-diagnostic allow rules must match the approved tool set: "
            f"{sorted(expected_actions)}"
        )
    for rule in allow_rules:
        if rule.resource != "*":
            raise McpPolicyError(
                "tv-mcp-diagnostic allow rules must use resource '*'"
            )
    for rule in rules:
        if rule.effect == "ask":
            raise McpPolicyError(
                "tv-mcp-diagnostic must not contain ask rules; use deny or allow"
            )


def _strip_jsonc_comments(text: str) -> str:
    output: list[str] = []
    index = 0
    in_string = False
    while index < len(text):
        char = text[index]
        if in_string:
            output.append(char)
            if char == "\\":
                index += 1
                if index < len(text):
                    output.append(text[index])
            elif char == '"':
                in_string = False
            index += 1
            continue

        if char == '"':
            in_string = True
            output.append(char)
            index += 1
            continue
        if char == "/" and index + 1 < len(text):
            next_char = text[index + 1]
            if next_char == "/":
                index += 2
                while index < len(text) and text[index] not in "\r\n":
                    index += 1
                continue
            if next_char == "*":
                index += 2
                closed = False
                while index < len(text):
                    if text[index] == "*" and index + 1 < len(text) and text[index + 1] == "/":
                        index += 2
                        closed = True
                        break
                    if text[index] in "\r\n":
                        output.append(text[index])
                    index += 1
                if not closed:
                    raise ValueError("unterminated block comment")
                continue
        output.append(char)
        index += 1
    return "".join(output)


def _strip_trailing_commas(text: str) -> str:
    output: list[str] = []
    in_string = False
    index = 0
    while index < len(text):
        char = text[index]
        if in_string:
            output.append(char)
            if char == "\\":
                index += 1
                if index < len(text):
                    output.append(text[index])
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            output.append(char)
            index += 1
            continue
        if char == ",":
            lookahead = index + 1
            while lookahead < len(text) and text[lookahead] in " \t\r\n":
                lookahead += 1
            if lookahead < len(text) and text[lookahead] in "}]":
                index += 1
                continue
        output.append(char)
        index += 1
    return "".join(output)


def _require_mapping(
    value: Any,
    label: str,
    *,
    allowed: frozenset[str],
    required: set[str] | frozenset[str] = frozenset(),
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise McpPolicyError(f"{label} must be an object")
    keys = set(value)
    missing = set(required) - keys
    if missing:
        raise McpPolicyError(f"{label} is missing keys: {sorted(missing)}")
    unknown = keys - allowed
    if unknown:
        raise McpPolicyError(f"{label} has unknown keys: {sorted(unknown)}")
    return value


def _require_nonempty_str(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise McpPolicyError(f"{label} must be a non-empty string")
    return value


def _require_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise McpPolicyError(f"{label} must be a positive integer")
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
