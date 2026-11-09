import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

from trading_automation.mcp.client import McpStdioClient
from trading_automation.mcp.errors import (
    McpPolicyError,
    McpProtocolError,
    McpTimeoutError,
    McpToolError,
    McpUnavailableError,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "fake_mcp_server.py"


class ClientTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)
        self.log = self.directory / "calls.jsonl"

    def client(self, *, mode="normal", environment=None, **kwargs):
        env = {"FAKE_MCP_MODE": mode, "FAKE_MCP_LOG": str(self.log)}
        env.update(environment or {})
        return McpStdioClient(
            command=(sys.executable, str(FIXTURE)),
            cwd=ROOT,
            environment=env,
            startup_timeout=kwargs.pop("startup_timeout", 10),
            catalog_timeout=kwargs.pop("catalog_timeout", 10),
            call_timeout=kwargs.pop("call_timeout", 10),
            shutdown_timeout=kwargs.pop("shutdown_timeout", 5),
            **kwargs,
        )

    def read_log(self):
        if not self.log.exists():
            return []
        return [
            json.loads(line)
            for line in self.log.read_text(encoding="utf-8").splitlines()
            if line
        ]

    def test_initialize_and_paginated_catalog(self):
        with self.client() as client:
            tools = client.list_tools()
            self.assertEqual(
                [tool.name for tool in tools],
                ["tv_health_check", "chart_get_state", "data_get_ohlcv"],
            )
            self.assertEqual(client.protocol_version, "2025-11-25")
            self.assertEqual(client.server_info["name"], "tradingview")
            self.assertEqual(client.server_info["version"], "2.0.0")
        methods = [entry["method"] for entry in self.read_log()]
        self.assertIn("initialize", methods)
        self.assertEqual(methods.count("tools/list"), 2)

    def test_single_page_catalog(self):
        with self.client(environment={"FAKE_MCP_PAGES": "1"}) as client:
            self.assertEqual(len(client.list_tools()), 3)
        self.assertEqual(
            [entry["method"] for entry in self.read_log()].count("tools/list"), 1
        )

    def test_disallowed_tool_is_blocked_before_send(self):
        with self.client() as client:
            with self.assertRaises(McpPolicyError):
                client.call_tool("tv_launch", {})
            payload = client.health()
            self.assertTrue(payload["success"])
        calls = [
            entry for entry in self.read_log() if entry["method"] == "tools/call"
        ]
        self.assertEqual([entry["params"]["name"] for entry in calls], ["tv_health_check"])

    def test_health_success(self):
        with self.client() as client:
            payload = client.health()
        self.assertEqual(payload["chart_symbol"], "EURUSD")

    def test_health_error_is_tool_error(self):
        with self.client(mode="health_error") as client:
            with self.assertRaises(McpToolError) as raised:
                client.health()
        self.assertIn("CDP connection failed", str(raised.exception))
        self.assertIn("TradingView is not running", str(raised.exception))

    def test_wrong_protocol_is_rejected(self):
        client = self.client(mode="wrong_protocol")
        with self.assertRaises(McpProtocolError):
            client.start()
            client.initialize()
        client.close()

    def test_bad_json_is_rejected(self):
        client = self.client(mode="bad_json")
        with self.assertRaises(McpProtocolError):
            client.start()
            client.initialize()
        client.close()

    def test_oversized_message_is_rejected(self):
        client = self.client(
            mode="oversized",
            max_message_bytes=1024,
            environment={"FAKE_MCP_OVERSIZE_BYTES": "4096"},
        )
        with self.assertRaises(McpProtocolError):
            client.start()
            client.initialize()
        client.close()

    def test_catalog_timeout(self):
        client = self.client(mode="hang", catalog_timeout=0.4)
        client.start()
        client.initialize()
        with self.assertRaises(McpTimeoutError):
            client.list_tools()
        client.close()
        self.assertFalse(client.is_running)

    def test_server_ping_is_answered(self):
        with self.client(mode="send_ping") as client:
            self.assertEqual(len(client.list_tools()), 3)
        methods = [entry["method"] for entry in self.read_log()]
        self.assertIn(None, methods)  # client response to the server's ping

    def test_call_requires_initialization(self):
        client = self.client()
        client.start()
        with self.assertRaises(McpProtocolError):
            client.list_tools()
        with self.assertRaises(McpProtocolError):
            client.call_tool("tv_health_check", {})
        client.close()

    def test_missing_executable_is_unavailable(self):
        client = McpStdioClient(
            command=("definitely-not-a-real-executable-xyz",),
            cwd=ROOT,
        )
        with self.assertRaises(McpUnavailableError):
            client.start()
        client.close()

    def test_close_is_idempotent(self):
        client = self.client()
        client.start()
        client.initialize()
        client.close()
        client.close()
        self.assertFalse(client.is_running)


if __name__ == "__main__":
    unittest.main()
