"""TradingView MCP boundary for Stage 2 (catalog and policy verification).

Live trading is not implemented.  This package only reads the pinned MCP
catalog and, when explicitly requested, the read-only ``tv_health_check``.
"""

from trading_automation.mcp.client import McpStdioClient, ToolInfo
from trading_automation.mcp.config import (
    AgentConfig,
    McpServerConfig,
    OpenCodeConfig,
    PermissionRule,
    load_agent_config,
    load_opencode_config,
    resolve_effect,
    verify_policy,
)
from trading_automation.mcp.errors import (
    McpConfigurationError,
    McpError,
    McpPolicyError,
    McpProtocolError,
    McpTimeoutError,
    McpToolError,
    McpUnavailableError,
)
from trading_automation.mcp.integrity import SourceState, verify_source_checkout
from trading_automation.mcp.manifest import (
    DEFAULT_EXPECTATIONS,
    EXPECTED_ALLOWED,
    EXPECTED_COMMIT,
    EXPECTED_TOTAL,
    ManifestExpectations,
    ToolEntry,
    ToolManifest,
    load_manifest,
    verify_catalog,
)

__all__ = [
    "AgentConfig",
    "DEFAULT_EXPECTATIONS",
    "EXPECTED_ALLOWED",
    "EXPECTED_COMMIT",
    "EXPECTED_TOTAL",
    "ManifestExpectations",
    "McpConfigurationError",
    "McpError",
    "McpPolicyError",
    "McpProtocolError",
    "McpServerConfig",
    "McpStdioClient",
    "McpTimeoutError",
    "McpToolError",
    "McpUnavailableError",
    "OpenCodeConfig",
    "PermissionRule",
    "SourceState",
    "ToolEntry",
    "ToolInfo",
    "ToolManifest",
    "load_agent_config",
    "load_manifest",
    "load_opencode_config",
    "resolve_effect",
    "verify_catalog",
    "verify_policy",
    "verify_source_checkout",
]
