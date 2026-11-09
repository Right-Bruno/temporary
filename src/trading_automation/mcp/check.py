"""Stage 2 MCP acceptance checks and exit-code mapping.

``check-mcp`` proves that the project configuration starts the pinned local
server, that the live catalog matches the 84-tool manifest, and that the
permission policy resolves to deny-by-default with exactly three diagnostic
allows.  ``--health`` additionally calls only ``tv_health_check``.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from trading_automation.mcp.client import McpStdioClient
from trading_automation.mcp.config import (
    load_agent_config,
    load_opencode_config,
    verify_policy,
)
from trading_automation.mcp.errors import (
    McpConfigurationError,
    McpPolicyError,
    McpProtocolError,
    McpTimeoutError,
    McpToolError,
    McpUnavailableError,
)
from trading_automation.mcp.integrity import SourceState, verify_source_checkout
from trading_automation.mcp.manifest import (
    DEFAULT_EXPECTATIONS,
    ManifestExpectations,
    ToolManifest,
    load_manifest,
    manifest_path,
    verify_catalog,
)

EXIT_MCP_OK = 0
EXIT_MCP_SOURCE_UNAVAILABLE = 20
EXIT_CHART_PRECONDITION = 21
EXIT_MARKET_DATA_VALIDATION = 22
EXIT_MCP_POLICY = 23

STATUS_OK = "ok"
STATUS_SOURCE_UNAVAILABLE = "source_unavailable"
STATUS_MCP_POLICY = "mcp_policy_or_protocol"
STATUS_INTERNAL_ERROR = "internal_error"


@dataclass(frozen=True, slots=True)
class CheckStep:
    name: str
    status: str
    detail: str


@dataclass(frozen=True, slots=True)
class CheckReport:
    status: str
    exit_code: int
    message: str
    steps: tuple[CheckStep, ...] = field(default_factory=tuple)
    details: Mapping[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.exit_code == EXIT_MCP_OK

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "status": self.status,
            "exit_code": self.exit_code,
            "message": self.message,
            "steps": [
                {
                    "name": step.name,
                    "status": step.status,
                    "detail": step.detail,
                }
                for step in self.steps
            ],
            "details": _jsonable(self.details),
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, ensure_ascii=False)

    def to_text(self) -> str:
        lines = [
            f"MCP check: {self.status.upper()} (exit {self.exit_code})",
            self.message,
        ]
        for step in self.steps:
            lines.append(f"[{step.status}] {step.name}: {step.detail}")
        return "\n".join(lines)


SourceVerifier = Callable[[ToolManifest, Path], SourceState]
ClientFactory = Callable[..., McpStdioClient]


def run_check(
    project_root: str | Path = ".",
    *,
    health: bool = False,
    expectations: ManifestExpectations = DEFAULT_EXPECTATIONS,
    source_verifier: SourceVerifier = verify_source_checkout,
    client_factory: ClientFactory = McpStdioClient,
) -> CheckReport:
    """Run the Stage 2 catalog/policy check and optional health probe."""

    root = Path(project_root).resolve()
    steps: list[CheckStep] = []
    details: dict[str, Any] = {}
    client: Any = None

    try:
        manifest = load_manifest(
            manifest_path(root), expectations=expectations
        )
        steps.append(
            CheckStep(
                "manifest",
                "ok",
                f"{len(manifest.tools)} tools; "
                f"{manifest.classification.read_only} read-only; "
                f"{manifest.classification.side_effecting} side-effecting; "
                f"{len(manifest.allowed_tools)} allowed",
            )
        )
        details["manifest"] = {
            "path": str(manifest_path(root)),
            "commit": manifest.source.commit,
            "server_version": manifest.source.server_version,
            "total": len(manifest.tools),
            "read_only": manifest.classification.read_only,
            "side_effecting": manifest.classification.side_effecting,
            "allowed": sorted(tool.name for tool in manifest.allowed_tools),
            "denied": len(manifest.denied_tools),
        }

        config = load_opencode_config(root)
        steps.append(
            CheckStep(
                "opencode_config",
                "ok",
                f"{config.server.command[0]} {config.server.command[1]} in "
                f"{config.server.cwd}; codemode=false; "
                f"CDP {config.server.environment['TV_CDP_HOST']}:"
                f"{config.server.environment['TV_CDP_PORT']}",
            )
        )
        details["opencode_config"] = {
            "path": str(config.path),
            "server": config.server.name,
            "command": list(config.server.command),
            "cwd": str(config.server.cwd),
            "codemode": config.server.codemode,
            "environment": dict(config.server.environment),
            "timeouts": dict(config.server.timeouts),
        }

        agent = load_agent_config(
            root, expected_allowed=expectations.allowed
        )
        verify_policy(config, agent, manifest, expectations=expectations)
        steps.append(
            CheckStep(
                "permission_policy",
                "ok",
                "top-level deny for all 84 tools; "
                f"{agent.name} allows exactly "
                f"{len(manifest.allowed_tools)} of {len(manifest.tools)}",
            )
        )
        details["permission_policy"] = {
            "top_level_rules": [_rule_dict(rule) for rule in config.permissions],
            "agent": agent.name,
            "agent_rules": [_rule_dict(rule) for rule in agent.permissions],
            "allowed": sorted(tool.opencode_name for tool in manifest.allowed_tools),
            "denied_count": len(manifest.denied_tools),
            "build_access": "none",
        }

        source = source_verifier(manifest, config.server.cwd)
        steps.append(
            CheckStep(
                "source_integrity",
                "ok",
                f"HEAD {source.commit}; tracked tree clean; "
                f"origin {source.repository_slug}; "
                f"{source.checked_source_files} source files present",
            )
        )
        details["source"] = {
            "root": str(source.root),
            "commit": source.commit,
            "clean_tracked_tree": source.clean_tracked_tree,
            "remote_url": source.remote_url,
            "repository_slug": source.repository_slug,
            "entrypoint": source.entrypoint,
            "checked_source_files": source.checked_source_files,
        }

        client = client_factory(
            command=config.server.command,
            cwd=source.root,
            environment=config.server.environment,
            startup_timeout=config.server.timeouts["startup"] / 1000,
            catalog_timeout=config.server.timeouts["catalog"] / 1000,
            call_timeout=config.server.timeouts["execution"] / 1000,
        )
        client.start()
        client.initialize()
        _verify_server_identity(client, manifest)
        steps.append(
            CheckStep(
                "handshake",
                "ok",
                f"{client.server_info['name']} "
                f"{client.server_info['version']}; protocol "
                f"{client.protocol_version}",
            )
        )
        details["handshake"] = {
            "server_name": client.server_info["name"],
            "server_version": client.server_info["version"],
            "protocol_version": client.protocol_version,
        }

        tools = client.list_tools()
        verify_catalog(
            manifest,
            [tool.name for tool in tools],
            expectations=expectations,
        )
        steps.append(
            CheckStep(
                "catalog",
                "ok",
                f"{len(tools)} tools match the pinned manifest exactly",
            )
        )
        details["catalog"] = {
            "count": len(tools),
            "exact_match": True,
            "names": [tool.name for tool in tools],
        }

        if health:
            payload = client.health()
            steps.append(
                CheckStep(
                    "health",
                    "ok",
                    "tv_health_check succeeded",
                )
            )
            details["health"] = dict(payload)
        else:
            steps.append(
                CheckStep(
                    "health",
                    "skipped",
                    "not requested; no TradingView/CDP call was made",
                )
            )

        return CheckReport(
            status=STATUS_OK,
            exit_code=EXIT_MCP_OK,
            message="MCP catalog, source pin, and permission policy are valid.",
            steps=tuple(steps),
            details=details,
        )
    except McpToolError as exc:
        steps.append(CheckStep("health", "failed", str(exc)))
        details["error"] = _error_details(exc)
        _add_stderr(details, client)
        return CheckReport(
            status=STATUS_SOURCE_UNAVAILABLE,
            exit_code=EXIT_MCP_SOURCE_UNAVAILABLE,
            message=(
                "TradingView/CDP is unavailable, so tv_health_check failed. "
                "This is the expected Stage 2 result when TradingView is closed."
            ),
            steps=tuple(steps),
            details=details,
        )
    except McpTimeoutError as exc:
        steps.append(CheckStep("mcp", "failed", str(exc)))
        details["error"] = _error_details(exc)
        _add_stderr(details, client)
        return CheckReport(
            status=STATUS_SOURCE_UNAVAILABLE,
            exit_code=EXIT_MCP_SOURCE_UNAVAILABLE,
            message="The MCP server or TradingView source timed out.",
            steps=tuple(steps),
            details=details,
        )
    except McpUnavailableError as exc:
        steps.append(CheckStep("mcp", "failed", str(exc)))
        details["error"] = _error_details(exc)
        _add_stderr(details, client)
        return CheckReport(
            status=STATUS_SOURCE_UNAVAILABLE,
            exit_code=EXIT_MCP_SOURCE_UNAVAILABLE,
            message="The pinned MCP source or server process is unavailable.",
            steps=tuple(steps),
            details=details,
        )
    except McpConfigurationError as exc:
        steps.append(CheckStep("configuration", "failed", str(exc)))
        details["error"] = _error_details(exc)
        return CheckReport(
            status=STATUS_MCP_POLICY,
            exit_code=EXIT_MCP_POLICY,
            message="The project MCP configuration is missing or invalid.",
            steps=tuple(steps),
            details=details,
        )
    except (McpPolicyError, McpProtocolError) as exc:
        steps.append(CheckStep("policy", "failed", str(exc)))
        details["error"] = _error_details(exc)
        _add_stderr(details, client)
        return CheckReport(
            status=STATUS_MCP_POLICY,
            exit_code=EXIT_MCP_POLICY,
            message="The MCP catalog, source pin, protocol, or permission policy failed.",
            steps=tuple(steps),
            details=details,
        )
    except OSError as exc:
        steps.append(CheckStep("mcp", "failed", str(exc)))
        details["error"] = _error_details(exc)
        _add_stderr(details, client)
        return CheckReport(
            status=STATUS_SOURCE_UNAVAILABLE,
            exit_code=EXIT_MCP_SOURCE_UNAVAILABLE,
            message="The MCP server process could not be reached.",
            steps=tuple(steps),
            details=details,
        )
    except Exception as exc:  # pragma: no cover - defensive boundary
        steps.append(CheckStep("internal", "failed", str(exc)))
        details["error"] = _error_details(exc)
        _add_stderr(details, client)
        return CheckReport(
            status=STATUS_INTERNAL_ERROR,
            exit_code=EXIT_MCP_POLICY,
            message="Unexpected failure while checking the MCP boundary.",
            steps=tuple(steps),
            details=details,
        )
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                pass


def _verify_server_identity(client: Any, manifest: ToolManifest) -> None:
    server_info = client.server_info
    if not isinstance(server_info, Mapping):
        raise McpProtocolError("MCP server identity is unavailable")
    if server_info.get("name") != manifest.source.server_name:
        raise McpPolicyError(
            f"MCP server name is {server_info.get('name')!r}; expected "
            f"{manifest.source.server_name!r}"
        )
    if server_info.get("version") != manifest.source.server_version:
        raise McpPolicyError(
            f"MCP server version is {server_info.get('version')!r}; expected "
            f"{manifest.source.server_version!r}"
        )


def _rule_dict(rule: Any) -> dict[str, str]:
    return {
        "action": rule.action,
        "resource": rule.resource,
        "effect": rule.effect,
    }


def _error_details(exc: BaseException) -> dict[str, str]:
    code = getattr(exc, "code", None)
    return {
        "type": type(exc).__name__,
        "code": str(code) if code is not None else "",
        "message": str(exc),
    }


def _add_stderr(details: dict[str, Any], client: Any) -> None:
    if client is None:
        return
    try:
        tail = client.stderr_tail(10)
    except Exception:
        return
    if tail:
        details["stderr_tail"] = tail


def _jsonable(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)
