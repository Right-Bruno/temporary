"""Typed failures at the TradingView MCP boundary.

Stage 2 keeps policy, protocol, and availability failures distinct so the CLI
can map them to explicit exit codes without inspecting message text.
"""

from __future__ import annotations


class McpError(Exception):
    """Base class for MCP boundary failures."""

    code = "MCP_ERROR"


class McpConfigurationError(McpError):
    """The local OpenCode/MCP configuration is missing or unsafe."""

    code = "MCP_CONFIGURATION"


class McpPolicyError(McpError):
    """The catalog, source pin, or tool allowlist violates the approved policy."""

    code = "MCP_POLICY"


class McpProtocolError(McpError):
    """The MCP peer violated the stdio JSON-RPC protocol."""

    code = "MCP_PROTOCOL"


class McpUnavailableError(McpError):
    """The MCP server process or its transport is unavailable."""

    code = "MCP_UNAVAILABLE"


class McpTimeoutError(McpUnavailableError):
    """The MCP server did not answer within the configured timeout."""

    code = "MCP_TIMEOUT"


class McpToolError(McpError):
    """A tool returned an error result."""

    code = "MCP_TOOL"
