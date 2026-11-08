"""Minimal fake MCP stdio server used by the Stage 2 client tests.

It implements only the JSON-RPC methods needed to exercise the Python client:
initialize, tools/list (with pagination), and tools/call.  It never touches
TradingView, CDP, SQLite, or the network.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

TOOLS = [
    {"name": "tv_health_check", "description": "health", "inputSchema": {"type": "object"}},
    {"name": "chart_get_state", "description": "state", "inputSchema": {"type": "object"}},
    {"name": "data_get_ohlcv", "description": "bars", "inputSchema": {"type": "object"}},
]


def _send(message: dict) -> None:
    sys.stdout.write(json.dumps(message, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _result(request_id: object, result: dict) -> None:
    _send({"jsonrpc": "2.0", "id": request_id, "result": result})


def _error(request_id: object, code: int, message: str) -> None:
    _send(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": code, "message": message},
        }
    )


def _log(entry: dict) -> None:
    path = os.environ.get("FAKE_MCP_LOG")
    if not path:
        return
    with Path(path).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, separators=(",", ":")) + "\n")


def _tool_text(payload: dict, *, is_error: bool = False) -> dict:
    return {
        "content": [{"type": "text", "text": json.dumps(payload)}],
        "isError": is_error,
    }


def main() -> int:
    mode = os.environ.get("FAKE_MCP_MODE", "normal")
    protocol = os.environ.get("FAKE_MCP_PROTOCOL", "2025-11-25")
    server_name = os.environ.get("FAKE_MCP_SERVER_NAME", "tradingview")
    server_version = os.environ.get("FAKE_MCP_SERVER_VERSION", "2.0.0")
    pages = max(1, int(os.environ.get("FAKE_MCP_PAGES", "2")))
    tool_names = os.environ.get(
        "FAKE_MCP_TOOLS", "tv_health_check,chart_get_state,data_get_ohlcv"
    )
    tools = [
        {"name": name, "description": name, "inputSchema": {"type": "object"}}
        for name in tool_names.split(",")
        if name
    ]
    page_size = max(1, (len(tools) + pages - 1) // pages)

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue

        method = message.get("method")
        request_id = message.get("id")
        _log({"method": method, "id": request_id, "params": message.get("params")})

        if method is None:
            # A response from the client to a server request (e.g. ping).
            continue

        if method == "initialize":
            if mode == "bad_json":
                sys.stdout.write("this is not json\n")
                sys.stdout.flush()
                continue
            if mode == "oversized":
                size = int(os.environ.get("FAKE_MCP_OVERSIZE_BYTES", "2097152"))
                sys.stdout.write("x" * size + "\n")
                sys.stdout.flush()
                continue
            _result(
                request_id,
                {
                    "protocolVersion": (
                        "1900-01-01" if mode == "wrong_protocol" else protocol
                    ),
                    "capabilities": {"tools": {}},
                    "serverInfo": {
                        "name": (
                            "other-server" if mode == "wrong_server" else server_name
                        ),
                        "version": server_version,
                    },
                },
            )
            if mode == "send_ping":
                _send({"jsonrpc": "2.0", "id": 999, "method": "ping"})
            continue

        if method == "notifications/initialized":
            continue

        if method == "tools/list":
            if mode == "hang":
                time.sleep(60)
                continue
            cursor = (message.get("params") or {}).get("cursor")
            start = int(cursor) if cursor else 0
            page = tools[start : start + page_size]
            result: dict = {"tools": page}
            next_start = start + page_size
            if next_start < len(tools):
                result["nextCursor"] = str(next_start)
            _result(request_id, result)
            continue

        if method == "tools/call":
            params = message.get("params") or {}
            name = params.get("name")
            if name == "tv_health_check":
                if mode == "hang_health":
                    time.sleep(60)
                elif mode == "health_error":
                    _result(
                        request_id,
                        _tool_text(
                            {
                                "success": False,
                                "error": "CDP connection failed after 5 attempts",
                                "hint": "TradingView is not running with CDP enabled.",
                            },
                            is_error=True,
                        ),
                    )
                else:
                    _result(
                        request_id,
                        _tool_text(
                            {
                                "success": True,
                                "cdp_connected": True,
                                "chart_symbol": "EURUSD",
                            }
                        ),
                    )
            elif name == "chart_get_state":
                _result(
                    request_id,
                    _tool_text({"success": True, "symbol": "EURUSD", "timeframe": "H1"}),
                )
            elif name == "data_get_ohlcv":
                _result(
                    request_id,
                    _tool_text({"success": True, "bars": [], "count": 0}),
                )
            else:
                _result(
                    request_id,
                    _tool_text(
                        {"success": False, "error": f"unknown tool {name}"},
                        is_error=True,
                    ),
                )
            continue

        _error(request_id, -32601, f"unknown method {method}")


if __name__ == "__main__":
    raise SystemExit(main())
