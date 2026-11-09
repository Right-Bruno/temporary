"""Command-line entry points for the local project foundation."""

from __future__ import annotations

import argparse
from pathlib import Path

from trading_automation import __version__
from trading_automation.config import load_settings
from trading_automation.data.csv_import import format_import_report, run_csv_import
from trading_automation.data.csv_validation import (
    format_validation_report,
    validate_csv,
)
from trading_automation.mcp.check import (
    EXIT_CHART_PRECONDITION,
    EXIT_MARKET_DATA_VALIDATION,
    EXIT_MCP_POLICY,
    EXIT_MCP_SOURCE_UNAVAILABLE,
    run_check,
)
from trading_automation.persistence.sqlite_store import SQLiteStore

EXIT_SUCCESS = 0
EXIT_VALIDATION_REJECTED = 10
EXIT_IMPORT_REJECTED = 11
EXIT_PERSISTENCE_FAILED = 12


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="trading-automation")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("status", help="show local configuration and safety state")
    subparsers.add_parser("init-db", help="create the local SQLite schema")

    validate_parser = subparsers.add_parser(
        "validate-csv",
        help="validate a historical OHLC CSV without writing to the database",
    )
    validate_parser.add_argument("path", help="path to the CSV file")
    validate_parser.add_argument(
        "--source-timezone",
        help="IANA timezone for naive source timestamps",
    )
    validate_parser.add_argument(
        "--json",
        action="store_true",
        help="print the validation report as JSON",
    )

    import_parser = subparsers.add_parser(
        "import-csv",
        help="validate and atomically import a historical OHLC CSV",
    )
    import_parser.add_argument("path", help="path to the CSV file")
    import_parser.add_argument(
        "--source-timezone",
        help="IANA timezone for naive source timestamps",
    )
    import_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="validate and preflight without writing candles or audit events",
    )
    import_parser.add_argument(
        "--json",
        action="store_true",
        help="print the import report as JSON",
    )

    check_parser = subparsers.add_parser(
        "check-mcp",
        help="verify the pinned TradingView MCP catalog and permission policy",
    )
    check_parser.add_argument(
        "--health",
        action="store_true",
        help=(
            "also call tv_health_check; exits 20 when TradingView/CDP is "
            "unavailable"
        ),
    )
    check_parser.add_argument(
        "--json",
        action="store_true",
        help="print the check report as JSON",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = Path.cwd()

    if args.command == "check-mcp":
        report = run_check(root, health=args.health)
        if args.json:
            print(report.to_json())
        else:
            print(report.to_text())
        return report.exit_code

    settings = load_settings(root)

    if args.command == "status":
        print(f"Trading Automation {__version__}")
        print(f"Mode: {settings.mode}")
        print(f"Timezone: {settings.timezone}")
        print(f"SQLite: {settings.sqlite_path}")
        print(f"Live execution enabled: {settings.live_execution_enabled}")
        print("Strategy engines: not implemented in this foundation")
        print("MT5 connection: not implemented")
        return 0

    if args.command == "init-db":
        store = SQLiteStore(settings.sqlite_path)
        store.initialise()
        print(f"Initialised SQLite database: {settings.sqlite_path}")
        return 0

    if args.command == "validate-csv":
        batch = validate_csv(
            args.path,
            source_timezone=args.source_timezone,
        )
        if args.json:
            print(batch.report.to_json())
        else:
            print(format_validation_report(batch.report))
        return (
            EXIT_SUCCESS
            if batch.report.status == "VALID"
            else EXIT_VALIDATION_REJECTED
        )

    if args.command == "import-csv":
        store = SQLiteStore(settings.sqlite_path)
        report = run_csv_import(
            args.path,
            store=store,
            source_timezone=args.source_timezone,
            dry_run=args.dry_run,
        )
        if args.json:
            print(report.to_json())
        else:
            print(format_import_report(report))
        if report.status in {"IMPORTED", "DRY_RUN_VALID"}:
            return EXIT_SUCCESS
        if report.status == "VALIDATION_REJECTED":
            return EXIT_VALIDATION_REJECTED
        if report.status == "IMPORT_REJECTED":
            return EXIT_IMPORT_REJECTED
        return EXIT_PERSISTENCE_FAILED

    return 2
