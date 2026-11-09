import copy
import json
import tempfile
import unittest
from pathlib import Path

from trading_automation.mcp.config import (
    AGENT_NAME,
    PermissionRule,
    agent_config_path,
    load_agent_config,
    load_jsonc,
    load_opencode_config,
    opencode_config_path,
    resolve_effect,
    verify_policy,
)
from trading_automation.mcp.errors import McpConfigurationError, McpPolicyError
from trading_automation.mcp.manifest import load_manifest, manifest_path

ROOT = Path(__file__).resolve().parents[1]


class ProjectFixture:
    def __init__(self, root: Path, config: dict, agent_text: str):
        self.root = root
        (root / "clone").mkdir()
        (root / ".opencode" / "agents").mkdir(parents=True)
        self.write_config(config)
        (root / ".opencode" / "agents" / f"{AGENT_NAME}.md").write_text(
            agent_text, encoding="utf-8"
        )

    def write_config(self, config: dict) -> None:
        (self.root / "opencode.jsonc").write_text(
            json.dumps(config, indent=2), encoding="utf-8"
        )

    def write_agent(self, text: str) -> None:
        (self.root / ".opencode" / "agents" / f"{AGENT_NAME}.md").write_text(
            text, encoding="utf-8"
        )


class PolicyTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)
        self.real_manifest = load_manifest(manifest_path(ROOT))
        self.real_config = load_jsonc(opencode_config_path(ROOT))
        self.real_agent_text = agent_config_path(ROOT).read_text(encoding="utf-8")

    def fixture(self, config=None, agent_text=None) -> ProjectFixture:
        document = copy.deepcopy(config or self.real_config)
        text = agent_text if agent_text is not None else self.real_agent_text
        return ProjectFixture(self.directory, document, text)

    def test_real_opencode_config_is_valid(self):
        config = load_opencode_config(ROOT)
        self.assertEqual(config.server.name, "tradingview")
        self.assertEqual(config.server.command, ("node", "src/server.js"))
        self.assertFalse(config.server.codemode)
        self.assertEqual(config.server.environment["TV_CDP_HOST"], "127.0.0.1")
        self.assertEqual(config.server.environment["TV_CDP_PORT"], "9222")
        self.assertEqual(config.server.timeouts, {
            "startup": 30000,
            "catalog": 30000,
            "execution": 60000,
        })
        self.assertEqual(len(config.permissions), 3)

    def test_real_agent_config_is_valid(self):
        agent = load_agent_config(ROOT)
        self.assertEqual(agent.name, AGENT_NAME)
        self.assertEqual(agent.mode, "subagent")
        self.assertEqual(len(agent.permissions), 4)
        self.assertEqual(
            agent.permissions[0],
            PermissionRule(action="*", resource="*", effect="deny"),
        )
        allows = [rule for rule in agent.permissions if rule.effect == "allow"]
        self.assertEqual(len(allows), 3)
        self.assertEqual(
            {rule.action for rule in allows},
            {
                "tradingview_tv_health_check",
                "tradingview_chart_get_state",
                "tradingview_data_get_ohlcv",
            },
        )

    def test_real_policy_verifies_deny_by_default(self):
        verify_policy(
            load_opencode_config(ROOT),
            load_agent_config(ROOT),
            self.real_manifest,
        )

    def test_effective_effects_are_exactly_three_allows(self):
        config = load_opencode_config(ROOT)
        agent = load_agent_config(ROOT)
        top_rules = list(config.permissions)
        agent_rules = list(agent.permissions)
        allowed = []
        for tool in self.real_manifest.tools:
            top = resolve_effect(top_rules, tool.opencode_name, "*")
            self.assertEqual(top, "deny", tool.name)
            effect = resolve_effect(top_rules + agent_rules, tool.opencode_name, "*")
            if effect == "allow":
                allowed.append(tool.name)
            else:
                self.assertEqual(effect, "deny", tool.name)
        self.assertEqual(
            set(allowed), {"tv_health_check", "chart_get_state", "data_get_ohlcv"}
        )

    def test_resolve_effect_uses_last_match(self):
        rules = [
            PermissionRule("tradingview_*", "*", "deny"),
            PermissionRule("tradingview_tv_health_check", "*", "allow"),
        ]
        self.assertEqual(
            resolve_effect(rules, "tradingview_tv_health_check", "*"), "allow"
        )
        self.assertEqual(
            resolve_effect(rules, "tradingview_tv_launch", "*"), "deny"
        )
        self.assertEqual(resolve_effect([], "tradingview_tv_launch", "*"), "ask")

    def test_relative_cwd_is_rejected(self):
        config = copy.deepcopy(self.real_config)
        config["mcp"]["servers"]["tradingview"]["cwd"] = "clone"
        self.fixture(config)
        with self.assertRaises(McpPolicyError):
            load_opencode_config(self.directory)

    def test_npx_command_is_rejected(self):
        config = copy.deepcopy(self.real_config)
        config["mcp"]["servers"]["tradingview"]["command"] = [
            "npx",
            "-y",
            "tradingview-mcp",
        ]
        self.fixture(config)
        with self.assertRaises(McpPolicyError):
            load_opencode_config(self.directory)

    def test_codemode_true_is_rejected(self):
        config = copy.deepcopy(self.real_config)
        config["mcp"]["servers"]["tradingview"]["codemode"] = True
        self.fixture(config)
        with self.assertRaises(McpPolicyError):
            load_opencode_config(self.directory)

    def test_wrong_cdp_host_is_rejected(self):
        config = copy.deepcopy(self.real_config)
        config["mcp"]["servers"]["tradingview"]["environment"]["TV_CDP_HOST"] = (
            "0.0.0.0"
        )
        self.fixture(config)
        with self.assertRaises(McpPolicyError):
            load_opencode_config(self.directory)

    def test_extra_environment_variable_is_rejected(self):
        config = copy.deepcopy(self.real_config)
        config["mcp"]["servers"]["tradingview"]["environment"]["CDP_PORT"] = "9333"
        self.fixture(config)
        with self.assertRaises(McpPolicyError):
            load_opencode_config(self.directory)

    def test_disabled_server_is_rejected(self):
        config = copy.deepcopy(self.real_config)
        config["mcp"]["servers"]["tradingview"]["disabled"] = True
        self.fixture(config)
        with self.assertRaises(McpPolicyError):
            load_opencode_config(self.directory)

    def test_protocol_auto_is_rejected(self):
        config = copy.deepcopy(self.real_config)
        config["mcp"]["servers"]["tradingview"]["protocol"] = "auto"
        self.fixture(config)
        with self.assertRaises(McpPolicyError):
            load_opencode_config(self.directory)

    def test_wrong_timeout_is_rejected(self):
        config = copy.deepcopy(self.real_config)
        config["mcp"]["timeout"]["execution"] = 3_600_000
        self.fixture(config)
        with self.assertRaises(McpPolicyError):
            load_opencode_config(self.directory)

    def test_extra_server_is_rejected(self):
        config = copy.deepcopy(self.real_config)
        config["mcp"]["servers"]["other"] = {
            "type": "local",
            "command": ["node", "other.js"],
        }
        self.fixture(config)
        with self.assertRaises(McpPolicyError):
            load_opencode_config(self.directory)

    def test_missing_top_level_deny_is_rejected(self):
        config = copy.deepcopy(self.real_config)
        config["permissions"] = config["permissions"][1:]
        self.fixture(config)
        with self.assertRaises(McpPolicyError):
            load_opencode_config(self.directory)

    def test_top_level_allow_is_rejected(self):
        config = copy.deepcopy(self.real_config)
        config["permissions"].append(
            {
                "action": "tradingview_tv_health_check",
                "resource": "*",
                "effect": "allow",
            }
        )
        self.fixture(config)
        with self.assertRaises(McpPolicyError):
            load_opencode_config(self.directory)

    def test_agent_missing_deny_all_is_rejected(self):
        text = self.real_agent_text.replace(
            "  - action: \"*\"\n    resource: \"*\"\n    effect: deny\n",
            "",
        )
        self.fixture(agent_text=text)
        with self.assertRaises(McpPolicyError):
            load_agent_config(self.directory)

    def test_agent_extra_allow_is_rejected(self):
        text = self.real_agent_text.replace(
            "  - action: tradingview_data_get_ohlcv\n"
            "    resource: \"*\"\n"
            "    effect: allow\n",
            "  - action: tradingview_data_get_ohlcv\n"
            "    resource: \"*\"\n"
            "    effect: allow\n"
            "  - action: tradingview_tv_launch\n"
            "    resource: \"*\"\n"
            "    effect: allow\n",
        )
        self.fixture(agent_text=text)
        with self.assertRaises(McpPolicyError):
            load_agent_config(self.directory)

    def test_agent_wrong_allow_is_rejected(self):
        text = self.real_agent_text.replace(
            "  - action: tradingview_data_get_ohlcv\n",
            "  - action: tradingview_tv_launch\n",
        )
        self.fixture(agent_text=text)
        with self.assertRaises(McpPolicyError):
            load_agent_config(self.directory)

    def test_agent_wrong_mode_is_rejected(self):
        text = self.real_agent_text.replace("mode: subagent", "mode: primary")
        self.fixture(agent_text=text)
        with self.assertRaises(McpPolicyError):
            load_agent_config(self.directory)

    def test_agent_ask_rule_is_rejected(self):
        text = self.real_agent_text.replace(
            "    effect: allow\n", "    effect: ask\n", 1
        )
        self.fixture(agent_text=text)
        with self.assertRaises(McpPolicyError):
            load_agent_config(self.directory)

    def test_agent_missing_permissions_is_rejected(self):
        text = self.real_agent_text.split("---", 2)[2]
        self.fixture(agent_text="---\ndescription: test\nmode: subagent\n---\n" + text)
        with self.assertRaises(McpPolicyError):
            load_agent_config(self.directory)

    def test_verify_policy_rejects_agent_extra_allow(self):
        extra = PermissionRule("tradingview_tv_launch", "*", "allow")
        agent = load_agent_config(ROOT)
        from dataclasses import replace

        tampered = replace(agent, permissions=agent.permissions + (extra,))
        with self.assertRaises(McpPolicyError):
            verify_policy(load_opencode_config(ROOT), tampered, self.real_manifest)


class JsoncTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)

    def write(self, text: str) -> Path:
        path = self.directory / "config.jsonc"
        path.write_text(text, encoding="utf-8")
        return path

    def test_comments_trailing_commas_and_urls(self):
        path = self.write(
            """
            {
              // line comment
              "url": "https://example.com/a//b",
              /* block
                 comment */
              "values": [1, 2,],
              "nested": {"ok": true,},
            }
            """
        )
        self.assertEqual(
            load_jsonc(path),
            {
                "url": "https://example.com/a//b",
                "values": [1, 2],
                "nested": {"ok": True},
            },
        )

    def test_duplicate_key_is_rejected(self):
        with self.assertRaises(McpConfigurationError):
            load_jsonc(self.write('{"a": 1, "a": 2}'))

    def test_non_finite_number_is_rejected(self):
        with self.assertRaises(McpConfigurationError):
            load_jsonc(self.write('{"a": NaN}'))

    def test_unterminated_block_comment_is_rejected(self):
        with self.assertRaises(McpConfigurationError):
            load_jsonc(self.write('{"a": 1 /* never closed}'))


if __name__ == "__main__":
    unittest.main()
