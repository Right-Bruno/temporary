import copy
import json
import tempfile
import unittest
from pathlib import Path

from trading_automation.mcp.errors import McpConfigurationError, McpPolicyError
from trading_automation.mcp.manifest import (
    EXPECTED_ALLOWED,
    EXPECTED_COMMIT,
    EXPECTED_TOTAL,
    load_manifest,
    manifest_path,
    verify_catalog,
)

ROOT = Path(__file__).resolve().parents[1]


class ManifestLoaderTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)
        self.real_path = manifest_path(ROOT)
        self.raw = json.loads(self.real_path.read_text(encoding="utf-8"))

    def write(self, document) -> Path:
        path = self.directory / "mcp-tools.json"
        path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
        return path

    def test_real_manifest_matches_approved_policy(self):
        manifest = load_manifest(self.real_path)
        self.assertEqual(len(manifest.tools), EXPECTED_TOTAL)
        self.assertEqual(manifest.source.commit, EXPECTED_COMMIT)
        self.assertEqual(manifest.classification.read_only, 31)
        self.assertEqual(manifest.classification.side_effecting, 53)
        self.assertEqual(
            {tool.name for tool in manifest.allowed_tools}, EXPECTED_ALLOWED
        )
        self.assertEqual(len(manifest.denied_tools), 81)
        self.assertEqual(
            {tool.opencode_name for tool in manifest.allowed_tools},
            {f"tradingview_{name}" for name in EXPECTED_ALLOWED},
        )

    def test_verify_catalog_accepts_exact_names(self):
        manifest = load_manifest(self.real_path)
        verify_catalog(manifest, [tool.name for tool in manifest.tools])

    def test_verify_catalog_rejects_missing_extra_and_duplicates(self):
        manifest = load_manifest(self.real_path)
        names = [tool.name for tool in manifest.tools]
        with self.assertRaises(McpPolicyError):
            verify_catalog(manifest, names[:-1])
        with self.assertRaises(McpPolicyError):
            verify_catalog(manifest, names + ["unexpected_tool"])
        with self.assertRaises(McpPolicyError):
            verify_catalog(manifest, names + [names[0]])

    def test_missing_file_is_configuration_error(self):
        with self.assertRaises(McpConfigurationError):
            load_manifest(self.directory / "absent.json")

    def test_malformed_json_is_policy_error(self):
        path = self.directory / "bad.json"
        path.write_text("{not json", encoding="utf-8")
        with self.assertRaises(McpPolicyError):
            load_manifest(path)

    def test_duplicate_json_key_is_rejected(self):
        path = self.directory / "duplicate.json"
        path.write_text(
            '{"schema_version": 1, "schema_version": 1}', encoding="utf-8"
        )
        with self.assertRaises(McpPolicyError):
            load_manifest(path)

    def test_wrong_commit_is_rejected(self):
        self.raw["source"]["commit"] = "0" * 40
        with self.assertRaises(McpPolicyError):
            load_manifest(self.write(self.raw))

    def test_wrong_server_version_is_rejected(self):
        self.raw["source"]["server_version"] = "9.9.9"
        with self.assertRaises(McpPolicyError):
            load_manifest(self.write(self.raw))

    def test_unknown_top_level_key_is_rejected(self):
        self.raw["extra"] = True
        with self.assertRaises(McpPolicyError):
            load_manifest(self.write(self.raw))

    def test_unknown_tool_key_is_rejected(self):
        self.raw["tools"][0]["extra"] = "value"
        with self.assertRaises(McpPolicyError):
            load_manifest(self.write(self.raw))

    def test_duplicate_tool_name_is_rejected(self):
        self.raw["tools"][1]["name"] = self.raw["tools"][0]["name"]
        self.raw["tools"][1]["opencode_name"] = (
            "tradingview_" + self.raw["tools"][0]["name"]
        )
        with self.assertRaises(McpPolicyError):
            load_manifest(self.write(self.raw))

    def test_allowed_tool_must_be_read_only(self):
        tool = next(item for item in self.raw["tools"] if item["name"] == "tv_health_check")
        tool["risk"] = "side_effecting"
        with self.assertRaises(McpPolicyError):
            load_manifest(self.write(self.raw))

    def test_non_approved_tool_cannot_be_allowed(self):
        tool = next(
            item for item in self.raw["tools"] if item["name"] == "tv_launch"
        )
        tool["phase1_access"] = "allowed"
        with self.assertRaises(McpPolicyError):
            load_manifest(self.write(self.raw))

    def test_opencode_name_must_be_normalized(self):
        self.raw["tools"][0]["opencode_name"] = "wrong_name"
        with self.assertRaises(McpPolicyError):
            load_manifest(self.write(self.raw))

    def test_source_file_must_stay_under_tools(self):
        self.raw["tools"][0]["source_file"] = "../server.js"
        with self.assertRaises(McpPolicyError):
            load_manifest(self.write(self.raw))

    def test_criterion_cannot_be_weakened(self):
        self.raw["classification"]["criterion"] = "Anything that returns JSON."
        with self.assertRaises(McpPolicyError):
            load_manifest(self.write(self.raw))

    def test_allowed_count_must_match(self):
        self.raw["phase1_access"]["allowed_count"] = 4
        with self.assertRaises(McpPolicyError):
            load_manifest(self.write(self.raw))

    def test_allowed_set_cannot_be_widened(self):
        self.raw["phase1_access"]["allowed"].append("tv_launch")
        with self.assertRaises(McpPolicyError):
            load_manifest(self.write(self.raw))

    def test_classification_counts_must_match_tools(self):
        self.raw["classification"]["read_only"] = 30
        with self.assertRaises(McpPolicyError):
            load_manifest(self.write(self.raw))


if __name__ == "__main__":
    unittest.main()
