import json
import shutil
import tempfile
import unittest
from pathlib import Path

from trading_automation.mcp.check import (
    EXIT_MCP_POLICY,
    EXIT_MCP_SOURCE_UNAVAILABLE,
    run_check,
)
from trading_automation.mcp.client import ToolInfo
from trading_automation.mcp.config import AGENT_NAME, load_jsonc
from trading_automation.mcp.errors import McpPolicyError, McpToolError, McpUnavailableError
from trading_automation.mcp.integrity import SourceState
from trading_automation.mcp.manifest import load_manifest, manifest_path

ROOT = Path(__file__).resolve().parents[1]


class FakeClient:
    server_info = {"name": "tradingview", "version": "2.0.0"}
    protocol_version = "2025-11-25"

    def __init__(self, manifest, *, health=None, catalog=None):
        self.manifest = manifest
        self.health_result = health
        self.catalog = catalog
        self.health_calls = 0
        self.closed = False
        self.started = False
        self.initialized = False

    def start(self):
        self.started = True

    def initialize(self):
        self.initialized = True

    def list_tools(self):
        names = self.catalog
        if names is None:
            names = [tool.name for tool in self.manifest.tools]
        return tuple(
            ToolInfo(name=name, description=name, input_schema={})
            for name in names
        )

    def health(self):
        self.health_calls += 1
        if isinstance(self.health_result, BaseException):
            raise self.health_result
        return self.health_result or {"success": True, "cdp_connected": True}

    def stderr_tail(self, limit=20):
        return ""

    def close(self):
        self.closed = True


class CheckTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.clone = self.root / "clone"
        self.clone.mkdir()
        (self.root / "config").mkdir()
        (self.root / ".opencode" / "agents").mkdir(parents=True)
        shutil.copyfile(
            manifest_path(ROOT),
            self.root / "config" / "mcp-tools.json",
        )
        config = load_jsonc(ROOT / "opencode.jsonc")
        config["mcp"]["servers"]["tradingview"]["cwd"] = str(self.clone)
        (self.root / "opencode.jsonc").write_text(
            json.dumps(config, indent=2), encoding="utf-8"
        )
        shutil.copyfile(
            ROOT / ".opencode" / "agents" / f"{AGENT_NAME}.md",
            self.root / ".opencode" / "agents" / f"{AGENT_NAME}.md",
        )

    def source_state(self, manifest, root):
        return SourceState(
            root=Path(root),
            commit=manifest.source.commit,
            clean_tracked_tree=True,
            remote_url=manifest.source.repository,
            repository_slug="right-bruno/tradingview-mcp",
            entrypoint=manifest.source.entrypoint,
            checked_source_files=len(manifest.tools),
        )

    def verify_source(self, manifest, root):
        return self.source_state(manifest, root)

    def run_with(self, client, *, health=False):
        return run_check(
            self.root,
            health=health,
            source_verifier=self.verify_source,
            client_factory=lambda **kwargs: client,
        )

    def manifest(self):
        return load_manifest(self.root / "config" / "mcp-tools.json")

    def test_catalog_check_passes_without_health_call(self):
        client = FakeClient(self.manifest())
        report = self.run_with(client)
        self.assertEqual(report.exit_code, 0)
        self.assertEqual(report.status, "ok")
        self.assertEqual(client.health_calls, 0)
        self.assertTrue(client.closed)
        self.assertIn("catalog", [step.name for step in report.steps])
        self.assertIn("skipped", [step.status for step in report.steps])

    def test_health_success_passes(self):
        client = FakeClient(self.manifest())
        report = self.run_with(client, health=True)
        self.assertEqual(report.exit_code, 0)
        self.assertEqual(client.health_calls, 1)
        self.assertIn("health", report.details)

    def test_health_tool_error_exits_source_unavailable(self):
        client = FakeClient(
            self.manifest(),
            health=McpToolError("CDP connection failed"),
        )
        report = self.run_with(client, health=True)
        self.assertEqual(report.exit_code, EXIT_MCP_SOURCE_UNAVAILABLE)
        self.assertEqual(report.status, "source_unavailable")
        self.assertEqual(client.health_calls, 1)
        self.assertTrue(client.closed)

    def test_catalog_mismatch_exits_policy(self):
        client = FakeClient(self.manifest(), catalog=["tv_health_check"])
        report = self.run_with(client)
        self.assertEqual(report.exit_code, EXIT_MCP_POLICY)
        self.assertEqual(report.status, "mcp_policy_or_protocol")

    def test_source_unavailable_exits_20(self):
        client = FakeClient(self.manifest())

        def unavailable(manifest, root):
            raise McpUnavailableError("clone missing")

        report = run_check(
            self.root,
            source_verifier=unavailable,
            client_factory=lambda **kwargs: client,
        )
        self.assertEqual(report.exit_code, EXIT_MCP_SOURCE_UNAVAILABLE)

    def test_source_policy_failure_exits_23(self):
        client = FakeClient(self.manifest())

        def wrong_commit(manifest, root):
            raise McpPolicyError("wrong commit")

        report = run_check(
            self.root,
            source_verifier=wrong_commit,
            client_factory=lambda **kwargs: client,
        )
        self.assertEqual(report.exit_code, EXIT_MCP_POLICY)

    def test_missing_manifest_exits_23(self):
        manifest = self.manifest()
        (self.root / "config" / "mcp-tools.json").unlink()
        client = FakeClient(manifest)
        report = self.run_with(client)
        self.assertEqual(report.exit_code, EXIT_MCP_POLICY)
        self.assertIn("configuration", [step.name for step in report.steps])

    def test_top_level_policy_tamper_exits_23(self):
        config = load_jsonc(self.root / "opencode.jsonc")
        config["permissions"] = [
            rule
            for rule in config["permissions"]
            if rule["action"] != "tradingview_*"
        ]
        (self.root / "opencode.jsonc").write_text(
            json.dumps(config, indent=2), encoding="utf-8"
        )
        client = FakeClient(self.manifest())
        report = self.run_with(client)
        self.assertEqual(report.exit_code, EXIT_MCP_POLICY)
        self.assertIn("policy", [step.name for step in report.steps])

    def test_report_json_and_text_are_stable(self):
        client = FakeClient(self.manifest())
        report = self.run_with(client)
        parsed = json.loads(report.to_json())
        self.assertEqual(parsed["schema_version"], 1)
        self.assertEqual(parsed["exit_code"], 0)
        self.assertTrue(parsed["details"]["catalog"]["exact_match"])
        text = report.to_text()
        self.assertIn("MCP check: OK", text)
        self.assertIn("catalog", text)


if __name__ == "__main__":
    unittest.main()
