import shutil
import unittest
from pathlib import Path

from trading_automation.mcp.check import run_check
from trading_automation.mcp.client import McpStdioClient
from trading_automation.mcp.config import load_opencode_config
from trading_automation.mcp.manifest import (
    load_manifest,
    manifest_path,
    verify_catalog,
)

ROOT = Path(__file__).resolve().parents[1]


class RealMcpHandshakeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if shutil.which("node") is None:
            raise unittest.SkipTest("node is not available")
        if not (ROOT / "opencode.jsonc").exists():
            raise unittest.SkipTest("opencode.jsonc is not present")
        cls.config = load_opencode_config(ROOT)
        if not (cls.config.server.cwd / ".git").exists():
            raise unittest.SkipTest("verified TradingView MCP clone is not present")
        cls.manifest = load_manifest(manifest_path(ROOT))

    def test_catalog_matches_pinned_manifest(self):
        with McpStdioClient(
            command=self.config.server.command,
            cwd=self.config.server.cwd,
            environment=self.config.server.environment,
            startup_timeout=30,
            catalog_timeout=30,
        ) as client:
            tools = client.list_tools()
            verify_catalog(self.manifest, [tool.name for tool in tools])
            self.assertEqual(client.server_info["name"], "tradingview")
            self.assertEqual(client.server_info["version"], "2.0.0")
            self.assertEqual(client.protocol_version, "2025-11-25")

    def test_full_check_without_health(self):
        report = run_check(ROOT)
        self.assertEqual(report.exit_code, 0, report.to_text())
        self.assertEqual(report.status, "ok")


if __name__ == "__main__":
    unittest.main()
