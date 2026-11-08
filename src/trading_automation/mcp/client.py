"""Minimal, strict MCP stdio client for the pinned TradingView server.

The client speaks newline-delimited JSON-RPC 2.0 over stdin/stdout, performs
the MCP initialize handshake, paginates ``tools/list``, and enforces the
Phase 1 allowlist *before* any ``tools/call`` message is written.  It does not
launch TradingView, connect to CDP, or call any tool outside the approved set.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from trading_automation import __version__
from trading_automation.mcp.errors import (
    McpPolicyError,
    McpProtocolError,
    McpTimeoutError,
    McpToolError,
    McpUnavailableError,
)
from trading_automation.mcp.manifest import EXPECTED_ALLOWED

CLIENT_NAME = "trading-automation-stage2"
CLIENT_PROTOCOL_VERSION = "2025-11-25"
SUPPORTED_PROTOCOL_VERSIONS = frozenset(
    {
        "2025-11-25",
        "2025-06-18",
        "2025-03-26",
        "2024-11-05",
        "2024-10-07",
    }
)
DEFAULT_STARTUP_TIMEOUT = 30.0
DEFAULT_CATALOG_TIMEOUT = 30.0
DEFAULT_CALL_TIMEOUT = 60.0
DEFAULT_SHUTDOWN_TIMEOUT = 5.0
DEFAULT_MAX_MESSAGE_BYTES = 8 * 1024 * 1024
DEFAULT_MAX_MESSAGES = 2_048
DEFAULT_MAX_STDERR_LINES = 200
DEFAULT_MAX_STDERR_LINE_BYTES = 64 * 1024
MAX_TOOL_PAGES = 50
MAX_TOOLS = 500
MAX_CALL_ARGUMENT_BYTES = 64 * 1024


@dataclass(frozen=True, slots=True)
class ToolInfo:
    name: str
    description: str
    input_schema: Mapping[str, Any]


class _EndOfStream:
    pass


@dataclass(frozen=True, slots=True)
class _ProtocolFault:
    message: str


_EOF = _EndOfStream()


class McpStdioClient:
    """A single-use MCP stdio connection."""

    def __init__(
        self,
        *,
        command: tuple[str, ...] | list[str],
        cwd: str | Path,
        environment: Mapping[str, str] | None = None,
        allowed_tools: frozenset[str] = EXPECTED_ALLOWED,
        startup_timeout: float = DEFAULT_STARTUP_TIMEOUT,
        catalog_timeout: float = DEFAULT_CATALOG_TIMEOUT,
        call_timeout: float = DEFAULT_CALL_TIMEOUT,
        shutdown_timeout: float = DEFAULT_SHUTDOWN_TIMEOUT,
        max_message_bytes: int = DEFAULT_MAX_MESSAGE_BYTES,
        max_messages: int = DEFAULT_MAX_MESSAGES,
        max_stderr_lines: int = DEFAULT_MAX_STDERR_LINES,
        max_stderr_line_bytes: int = DEFAULT_MAX_STDERR_LINE_BYTES,
    ) -> None:
        if not command or not all(isinstance(part, str) and part for part in command):
            raise ValueError("command must be a non-empty list of strings")
        if not Path(cwd).is_dir():
            raise ValueError(f"cwd is not a directory: {cwd}")
        if startup_timeout <= 0 or catalog_timeout <= 0 or call_timeout <= 0:
            raise ValueError("timeouts must be positive")
        if max_message_bytes < 1024:
            raise ValueError("max_message_bytes is too small")
        if max_messages < 1 or max_stderr_lines < 1:
            raise ValueError("message limits must be positive")

        self._command = tuple(command)
        self._cwd = Path(cwd)
        self._environment = dict(environment or {})
        self._allowed_tools = frozenset(allowed_tools)
        self._startup_timeout = startup_timeout
        self._catalog_timeout = catalog_timeout
        self._call_timeout = call_timeout
        self._shutdown_timeout = shutdown_timeout
        self._max_message_bytes = max_message_bytes
        self._max_messages = max_messages
        self._max_stderr_lines = max_stderr_lines
        self._max_stderr_line_bytes = max_stderr_line_bytes

        self._process: subprocess.Popen[bytes] | None = None
        self._stdin: Any = None
        self._stdout: Any = None
        self._stderr: Any = None
        self._queue: queue.Queue[Any] = queue.Queue()
        self._stderr_lines: deque[str] = deque(maxlen=max_stderr_lines)
        self._reader_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._write_lock = threading.Lock()
        self._next_id = 0
        self._messages_seen = 0
        self._protocol_fault: str | None = None
        self._initialized = False
        self._closed = False

        self.protocol_version: str | None = None
        self.server_info: Mapping[str, Any] | None = None
        self.capabilities: Mapping[str, Any] | None = None

    # ------------------------------------------------------------------ lifecycle

    def __enter__(self) -> McpStdioClient:
        self.start()
        self.initialize()
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()

    @property
    def is_running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    @property
    def pid(self) -> int | None:
        return self._process.pid if self._process is not None else None

    def start(self) -> None:
        if self._closed:
            raise McpUnavailableError("MCP client is closed")
        if self._process is not None:
            return

        environment = self._build_environment()
        creationflags = 0
        if os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

        try:
            process = subprocess.Popen(
                list(self._command),
                cwd=str(self._cwd),
                env=environment,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
                creationflags=creationflags,
            )
        except FileNotFoundError as exc:
            raise McpUnavailableError(
                f"MCP executable is not available: {self._command[0]}"
            ) from exc
        except OSError as exc:
            raise McpUnavailableError(
                f"Could not start MCP server: {exc}"
            ) from exc

        self._process = process
        self._stdin = process.stdin
        self._stdout = process.stdout
        self._stderr = process.stderr

        self._reader_thread = threading.Thread(
            target=self._reader_loop,
            name="mcp-stdout",
            daemon=True,
        )
        self._stderr_thread = threading.Thread(
            target=self._stderr_loop,
            name="mcp-stderr",
            daemon=True,
        )
        self._reader_thread.start()
        self._stderr_thread.start()

    def initialize(self) -> None:
        if self._initialized:
            return
        if self._process is None:
            raise McpUnavailableError("MCP client has not been started")

        result = self._request(
            "initialize",
            {
                "protocolVersion": CLIENT_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": CLIENT_NAME, "version": __version__},
            },
            timeout=self._startup_timeout,
        )
        if not isinstance(result, Mapping):
            raise McpProtocolError("initialize result must be an object")

        protocol_version = result.get("protocolVersion")
        if not isinstance(protocol_version, str):
            raise McpProtocolError("initialize result has no protocolVersion")
        if protocol_version not in SUPPORTED_PROTOCOL_VERSIONS:
            raise McpProtocolError(
                f"Server selected unsupported MCP protocol version "
                f"{protocol_version!r}"
            )

        server_info = result.get("serverInfo")
        if not isinstance(server_info, Mapping):
            raise McpProtocolError("initialize result has no serverInfo object")
        name = server_info.get("name")
        version = server_info.get("version")
        if not isinstance(name, str) or not name:
            raise McpProtocolError("initialize serverInfo.name is invalid")
        if not isinstance(version, str) or not version:
            raise McpProtocolError("initialize serverInfo.version is invalid")

        capabilities = result.get("capabilities")
        if capabilities is None:
            capabilities = {}
        if not isinstance(capabilities, Mapping):
            raise McpProtocolError("initialize capabilities must be an object")

        self.protocol_version = protocol_version
        self.server_info = dict(server_info)
        self.capabilities = dict(capabilities)
        self._initialized = True
        self._notify("notifications/initialized", {})

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        process = self._process
        if process is None:
            return

        if process.poll() is None:
            try:
                if self._stdin is not None:
                    self._stdin.close()
            except OSError:
                pass
            try:
                process.wait(timeout=self._shutdown_timeout)
            except subprocess.TimeoutExpired:
                try:
                    process.terminate()
                except OSError:
                    pass
                try:
                    process.wait(timeout=self._shutdown_timeout)
                except subprocess.TimeoutExpired:
                    try:
                        process.kill()
                    except OSError:
                        pass
                    try:
                        process.wait(timeout=self._shutdown_timeout)
                    except subprocess.TimeoutExpired:
                        pass

        for thread in (self._reader_thread, self._stderr_thread):
            if thread is not None:
                thread.join(timeout=self._shutdown_timeout)
        for stream in (self._stdout, self._stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
        self._stdin = None
        self._stdout = None
        self._stderr = None
        self._process = None

    # -------------------------------------------------------------------- catalog

    def list_tools(self) -> tuple[ToolInfo, ...]:
        self._require_initialized()
        tools: list[ToolInfo] = []
        seen: set[str] = set()
        cursor: str | None = None

        for _ in range(MAX_TOOL_PAGES):
            params: dict[str, Any] = {}
            if cursor is not None:
                params["cursor"] = cursor
            result = self._request(
                "tools/list", params, timeout=self._catalog_timeout
            )
            if not isinstance(result, Mapping):
                raise McpProtocolError("tools/list result must be an object")
            page = result.get("tools")
            if not isinstance(page, list):
                raise McpProtocolError("tools/list result has no tools array")

            for item in page:
                if not isinstance(item, Mapping):
                    raise McpProtocolError("tools/list contains a non-object tool")
                name = item.get("name")
                if not isinstance(name, str) or not name:
                    raise McpProtocolError("tools/list contains an invalid tool name")
                if name in seen:
                    raise McpProtocolError(
                        f"tools/list contains duplicate tool name {name!r}"
                    )
                seen.add(name)
                description = item.get("description", "")
                if description is None:
                    description = ""
                if not isinstance(description, str):
                    raise McpProtocolError(
                        f"tools/list description for {name!r} is not a string"
                    )
                schema = item.get("inputSchema", {})
                if schema is None:
                    schema = {}
                if not isinstance(schema, Mapping):
                    raise McpProtocolError(
                        f"tools/list inputSchema for {name!r} is not an object"
                    )
                tools.append(
                    ToolInfo(
                        name=name,
                        description=description,
                        input_schema=dict(schema),
                    )
                )
                if len(tools) > MAX_TOOLS:
                    raise McpProtocolError(
                        f"tools/list returned more than {MAX_TOOLS} tools"
                    )

            next_cursor = result.get("nextCursor")
            if next_cursor is None:
                return tuple(tools)
            if not isinstance(next_cursor, str) or not next_cursor:
                raise McpProtocolError("tools/list nextCursor is invalid")
            cursor = next_cursor

        raise McpProtocolError(
            f"tools/list exceeded {MAX_TOOL_PAGES} pages"
        )

    # ----------------------------------------------------------------------- calls

    def call_tool(
        self, name: str, arguments: Mapping[str, Any] | None = None
    ) -> Mapping[str, Any]:
        """Call one allowlisted tool.

        The allowlist check happens before serialization or writing, so a
        disallowed tool can never reach the server process.
        """

        self._require_initialized()
        if name not in self._allowed_tools:
            raise McpPolicyError(
                f"Tool {name!r} is not in the Phase 1 MCP allowlist"
            )
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, Mapping):
            raise McpProtocolError("Tool arguments must be an object")

        request = {
            "name": name,
            "arguments": dict(arguments),
        }
        encoded = self._encode(request)
        if len(encoded) > MAX_CALL_ARGUMENT_BYTES:
            raise McpProtocolError(
                f"Tool call arguments exceed {MAX_CALL_ARGUMENT_BYTES} bytes"
            )

        result = self._request(
            "tools/call", request, timeout=self._call_timeout
        )
        if not isinstance(result, Mapping):
            raise McpProtocolError("tools/call result must be an object")
        return result

    def health(self) -> Mapping[str, Any]:
        """Call ``tv_health_check`` and parse its JSON payload.

        A CDP/TradingView failure is surfaced as :class:`McpToolError`.  The
        caller maps that to the Stage 2 source-unavailable exit code.
        """

        result = self.call_tool("tv_health_check", {})
        payload = self._tool_payload(result, "tv_health_check")

        is_error = result.get("isError") is True
        if is_error or payload.get("success") is False:
            error = payload.get("error")
            hint = payload.get("hint")
            if not isinstance(error, str) or not error:
                error = "tv_health_check reported failure"
            message = error
            if isinstance(hint, str) and hint:
                message = f"{message} Hint: {hint}"
            raise McpToolError(message)
        if payload.get("success") is not True:
            raise McpProtocolError(
                "tv_health_check payload has no boolean success field"
            )
        return payload

    # --------------------------------------------------------------------- helpers

    def stderr_tail(self, limit: int = 20) -> str:
        if limit <= 0:
            return ""
        lines = list(self._stderr_lines)[-limit:]
        text = "\n".join(lines)
        if len(text) > 4_000:
            text = text[-4_000:]
        return text

    def _require_initialized(self) -> None:
        if not self._initialized:
            raise McpProtocolError("MCP client is not initialized")
        if self._process is None:
            raise McpUnavailableError("MCP server process is not running")
        if self._process.poll() is not None:
            raise McpUnavailableError(
                f"MCP server exited with code {self._process.returncode}"
            )

    def _build_environment(self) -> dict[str, str]:
        environment = dict(os.environ)
        for key in list(environment):
            normalized = key.upper()
            if normalized in {"CDP_HOST", "CDP_PORT"} or normalized.startswith(
                "TV_CDP_"
            ):
                del environment[key]
        environment.update(self._environment)
        return environment

    def _encode(self, message: Mapping[str, Any]) -> bytes:
        try:
            text = json.dumps(
                message, ensure_ascii=True, separators=(",", ":"), allow_nan=False
            )
        except (TypeError, ValueError) as exc:
            raise McpProtocolError(f"Could not encode MCP message: {exc}") from exc
        encoded = text.encode("utf-8")
        if len(encoded) > self._max_message_bytes:
            raise McpProtocolError(
                f"MCP message exceeds {self._max_message_bytes} bytes"
            )
        return encoded

    def _send(self, message: Mapping[str, Any]) -> None:
        if self._stdin is None:
            raise McpUnavailableError("MCP stdin is not available")
        encoded = self._encode(message)
        with self._write_lock:
            try:
                self._stdin.write(encoded + b"\n")
                self._stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                raise McpUnavailableError(
                    "MCP server closed its input stream"
                ) from exc

    def _notify(self, method: str, params: Mapping[str, Any]) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": dict(params)})

    def _request(
        self, method: str, params: Mapping[str, Any], *, timeout: float
    ) -> Any:
        self._next_id += 1
        request_id = self._next_id
        self._send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": dict(params),
            }
        )

        deadline = time.monotonic() + timeout
        try:
            while True:
                message = self._next_message(deadline)
                if "method" in message:
                    if "id" in message:
                        self._handle_server_request(message)
                    continue

                if message.get("id") != request_id:
                    raise McpProtocolError(
                        f"Unexpected JSON-RPC response id {message.get('id')!r} "
                        f"while waiting for {request_id}"
                    )
                if "error" in message:
                    error = message["error"]
                    if not isinstance(error, Mapping):
                        raise McpProtocolError("JSON-RPC error must be an object")
                    code = error.get("code", "unknown")
                    detail = error.get("message", "unknown error")
                    raise McpProtocolError(
                        f"MCP {method} failed with JSON-RPC error {code}: {detail}"
                    )
                if "result" not in message:
                    raise McpProtocolError(
                        f"JSON-RPC response for {method} has no result"
                    )
                return message["result"]
        except McpTimeoutError:
            try:
                self._notify(
                    "notifications/cancelled",
                    {"requestId": request_id, "reason": "timeout"},
                )
            except Exception:
                pass
            raise

    def _next_message(self, deadline: float) -> Mapping[str, Any]:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise McpTimeoutError("Timed out waiting for an MCP response")
        try:
            message = self._queue.get(timeout=remaining)
        except queue.Empty as exc:
            raise McpTimeoutError(
                "Timed out waiting for an MCP response"
            ) from exc

        if message is _EOF:
            tail = self.stderr_tail(5)
            detail = f"; stderr: {tail}" if tail else ""
            raise McpUnavailableError(f"MCP server closed stdout{detail}")
        if isinstance(message, _ProtocolFault):
            raise McpProtocolError(message.message)
        if not isinstance(message, Mapping):
            raise McpProtocolError("MCP message is not a JSON object")
        if message.get("jsonrpc") != "2.0":
            raise McpProtocolError("MCP message has no jsonrpc='2.0' field")
        return message

    def _handle_server_request(self, message: Mapping[str, Any]) -> None:
        request_id = message.get("id")
        method = message.get("method")
        if method == "ping":
            self._send({"jsonrpc": "2.0", "id": request_id, "result": {}})
            return
        if method == "roots/list":
            self._send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {"roots": []},
                }
            )
            return
        self._send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {
                    "code": -32601,
                    "message": f"Client does not support server method {method!r}",
                },
            }
        )

    @staticmethod
    def _tool_payload(
        result: Mapping[str, Any], tool_name: str
    ) -> Mapping[str, Any]:
        content = result.get("content")
        if not isinstance(content, list):
            raise McpProtocolError(f"{tool_name} result has no content array")
        text_parts: list[str] = []
        for item in content:
            if not isinstance(item, Mapping):
                continue
            if item.get("type") == "text" and isinstance(item.get("text"), str):
                text_parts.append(item["text"])
        if not text_parts:
            raise McpProtocolError(f"{tool_name} result has no text content")
        text = "\n".join(text_parts)
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            preview = text[:200].replace("\n", " ")
            raise McpProtocolError(
                f"{tool_name} returned non-JSON text: {preview!r}"
            ) from exc
        if not isinstance(payload, Mapping):
            raise McpProtocolError(f"{tool_name} payload is not a JSON object")
        return payload

    # ---------------------------------------------------------------------- threads

    def _reader_loop(self) -> None:
        stdout = self._stdout
        if stdout is None:
            return
        while True:
            try:
                chunk = stdout.readline(self._max_message_bytes + 1)
            except (OSError, ValueError):
                self._put_fault("Could not read from MCP stdout")
                return
            if not chunk:
                self._queue.put(_EOF)
                return
            if len(chunk) > self._max_message_bytes:
                self._put_fault(
                    f"MCP message exceeds {self._max_message_bytes} bytes"
                )
                return
            if not chunk.endswith(b"\n"):
                self._put_fault("MCP stdout ended in a partial message")
                return
            line = chunk.rstrip(b"\r\n")
            if not line:
                continue
            self._messages_seen += 1
            if self._messages_seen > self._max_messages:
                self._put_fault(
                    f"MCP server sent more than {self._max_messages} messages"
                )
                return
            try:
                text = line.decode("utf-8")
            except UnicodeDecodeError:
                self._put_fault("MCP stdout is not valid UTF-8")
                return
            try:
                message = json.loads(text)
            except json.JSONDecodeError as exc:
                preview = text[:200].replace("\n", " ")
                self._put_fault(
                    f"MCP stdout is not valid JSON ({exc.msg}): {preview!r}"
                )
                return
            self._queue.put(message)

    def _stderr_loop(self) -> None:
        stderr = self._stderr
        if stderr is None:
            return
        while True:
            try:
                chunk = stderr.readline(self._max_stderr_line_bytes + 1)
            except (OSError, ValueError):
                return
            if not chunk:
                return
            if len(chunk) > self._max_stderr_line_bytes:
                self._stderr_lines.append(
                    f"[stderr line truncated at {self._max_stderr_line_bytes} bytes]"
                )
                continue
            line = chunk.decode("utf-8", errors="replace").rstrip("\r\n")
            self._stderr_lines.append(line)

    def _put_fault(self, message: str) -> None:
        if self._protocol_fault is None:
            self._protocol_fault = message
        self._queue.put(_ProtocolFault(message))
