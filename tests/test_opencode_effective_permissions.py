import json
import os
import shutil
import subprocess
import unittest
from pathlib import Path

from trading_automation.mcp.config import PermissionRule, resolve_effect
from trading_automation.mcp.manifest import load_manifest, manifest_path

ROOT = Path(__file__).resolve().parents[1]


def find_opencode() -> str | None:
    candidate = shutil.which("opencode")
    if candidate:
        return candidate
    appdata = os.environ.get("APPDATA")
    if appdata:
        base = Path(appdata) / "ai.opencode.desktop" / "cli"
        if base.is_dir():
            matches = sorted(base.glob("*/opencode-cli.exe"))
            if matches:
                return str(matches[-1])
    return None


class EffectivePermissionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cli = find_opencode()
        if cls.cli is None:
            raise unittest.SkipTest("OpenCode CLI is not available")
        cls.manifest = load_manifest(manifest_path(ROOT))

    def test_build_has_no_tradingview_access_and_diagnostic_has_three(self):
        completed = subprocess.run(
            [self.cli, "debug", "agents"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
            shell=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        text = completed.stdout.strip()
        start = text.find("[")
        self.assertGreaterEqual(start, 0, text[:500])
        agents, _ = json.JSONDecoder().raw_decode(text[start:])
        by_id = {agent["id"]: agent for agent in agents}
        self.assertIn("build", by_id)
        self.assertIn("tv-mcp-diagnostic", by_id)

        results = {}
        for agent in agents:
            rules = tuple(
                PermissionRule(
                    action=rule["action"],
                    resource=rule["resource"],
                    effect=rule["effect"],
                )
                for rule in agent.get("permissions", [])
            )
            allowed = []
            denied = 0
            asked = 0
            for tool in self.manifest.tools:
                effect = resolve_effect(rules, tool.opencode_name, "*")
                if effect == "allow":
                    allowed.append(tool.name)
                elif effect == "deny":
                    denied += 1
                else:
                    asked += 1
            results[agent["id"]] = (allowed, denied, asked)

        self.assertEqual(results["build"], ([], 84, 0))
        self.assertEqual(
            set(results["tv-mcp-diagnostic"][0]),
            {"tv_health_check", "chart_get_state", "data_get_ohlcv"},
        )
        self.assertEqual(results["tv-mcp-diagnostic"][1:], (81, 0))
        for agent_id, (allowed, denied, asked) in results.items():
            if agent_id == "tv-mcp-diagnostic":
                continue
            self.assertEqual(allowed, [], agent_id)
            self.assertEqual(denied, 84, agent_id)
            self.assertEqual(asked, 0, agent_id)


if __name__ == "__main__":
    unittest.main()
