import io
import json
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from trading_automation.cli import EXIT_CHART_PRECONDITION, EXIT_MARKET_DATA_VALIDATION, main
from trading_automation.mcp.check import (
    EXIT_MCP_POLICY,
    EXIT_MCP_SOURCE_UNAVAILABLE,
    CheckReport,
    CheckStep,
)


def report(exit_code: int, status: str = "ok") -> CheckReport:
    return CheckReport(
        status=status,
        exit_code=exit_code,
        message="test report",
        steps=(CheckStep("catalog", "ok", "84 tools"),),
        details={"catalog": {"count": 84}},
    )


class CliMcpTests(unittest.TestCase):
    def run_main(self, argv):
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(argv)
        return code, output.getvalue()

    def test_check_mcp_defaults_to_catalog_only(self):
        with patch("trading_automation.cli.run_check", return_value=report(0)) as check:
            with patch(
                "trading_automation.cli.load_settings",
                side_effect=AssertionError("check-mcp must not load app settings"),
            ):
                code, output = self.run_main(["check-mcp"])
        self.assertEqual(code, 0)
        check.assert_called_once()
        self.assertFalse(check.call_args.kwargs["health"])
        self.assertIn("MCP check: OK", output)

    def test_check_mcp_health_flag_is_forwarded(self):
        with patch(
            "trading_automation.cli.run_check",
            return_value=report(
                EXIT_MCP_SOURCE_UNAVAILABLE, "source_unavailable"
            ),
        ) as check:
            code, output = self.run_main(["check-mcp", "--health"])
        self.assertEqual(code, EXIT_MCP_SOURCE_UNAVAILABLE)
        self.assertTrue(check.call_args.kwargs["health"])
        self.assertIn("SOURCE_UNAVAILABLE", output)

    def test_check_mcp_json_output(self):
        with patch("trading_automation.cli.run_check", return_value=report(0)):
            code, output = self.run_main(["check-mcp", "--json"])
        self.assertEqual(code, 0)
        parsed = json.loads(output)
        self.assertEqual(parsed["exit_code"], 0)
        self.assertEqual(parsed["details"]["catalog"]["count"], 84)

    def test_check_mcp_policy_exit_code(self):
        with patch(
            "trading_automation.cli.run_check",
            return_value=report(EXIT_MCP_POLICY, "mcp_policy_or_protocol"),
        ):
            code, _ = self.run_main(["check-mcp"])
        self.assertEqual(code, EXIT_MCP_POLICY)

    def test_reserved_exit_codes_are_distinct(self):
        self.assertEqual(EXIT_MCP_SOURCE_UNAVAILABLE, 20)
        self.assertEqual(EXIT_CHART_PRECONDITION, 21)
        self.assertEqual(EXIT_MARKET_DATA_VALIDATION, 22)
        self.assertEqual(EXIT_MCP_POLICY, 23)


if __name__ == "__main__":
    unittest.main()
