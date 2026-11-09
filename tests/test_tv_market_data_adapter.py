"""Stage 3 TradingView adapter tests using fake transport.

All tests inject a fake MCP client; no real TradingView or CDP connection is
made.  Call order and argument capture are asserted to prove the adapter
verifies chart state before requesting OHLCV data.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timezone
from decimal import Decimal

from trading_automation.data.market_data.contract import (
    CandleRequest,
    InstrumentNotMappedError,
    InstrumentRef,
    SourceBinding,
    SourcePreconditionError,
    SourceUnavailableError,
    Timeframe,
)
from trading_automation.data.market_data.tv_source import TvMarketDataSource
from trading_automation.domain.candles import Candle
from trading_automation.domain.timezones import to_new_york
from trading_automation.mcp.client import McpStdioClient
from tests.fixtures.fake_tv_transport import FakeMcpClient

_CANONICAL = InstrumentRef(
    canonical_id="EURUSD",
    asset_class="forex",
    display_name="Euro / US Dollar",
)

_BINDING = SourceBinding(
    adapter="tradingview",
    source_symbol="FOREXCOM:EURUSD",
    source_label="tradingview-mcp:FOREXCOM",
    status="VERIFIED",
)

_NOW = datetime(2026, 11, 8, 12, 0, 0, tzinfo=timezone.utc)


def _healthy_client() -> FakeMcpClient:
    return FakeMcpClient(
        health_response={"success": True},
        chart_states={
            "success": True,
            "symbol": "FOREXCOM:EURUSD",
            "resolution": "60",
        },
        ohlcv_response={
            "success": True,
            "bars": [
                {
                    "time": 1731076800,
                    "open": "1.0800",
                    "high": "1.0850",
                    "low": "1.0790",
                    "close": "1.0820",
                    "volume": "1000",
                },
                {
                    "time": 1731080400,
                    "open": "1.0820",
                    "high": "1.0860",
                    "low": "1.0810",
                    "close": "1.0840",
                    "volume": "1200",
                },
            ],
            "total_bars": 2,
            "source": "direct_bars",
        },
    )


def _request(timeframe: Timeframe = Timeframe.H1, count: int = 2) -> CandleRequest:
    return CandleRequest(
        instrument=_CANONICAL,
        timeframe=timeframe,
        count=count,
        source_filter="tradingview-mcp:FOREXCOM",
    )


class TvAdapterHappyPathTests(unittest.TestCase):
    def test_correct_symbol_and_timeframe_returns_series(self):
        client = _healthy_client()
        adapter = TvMarketDataSource(_CANONICAL, _BINDING, client, now=_NOW)
        series = adapter.get_candles(_request())

        self.assertIsInstance(series, __import__(
            "trading_automation.data.market_data.contract",
            fromlist=["CandleSeries"],
        ).CandleSeries)
        self.assertEqual(len(series.candles), 2)
        self.assertEqual(series.instrument, _CANONICAL)
        self.assertEqual(series.timeframe, Timeframe.H1)
        self.assertEqual(series.metadata.adapter_name, "tradingview")
        self.assertEqual(series.metadata.sources, ("tradingview-mcp:FOREXCOM",))
        self.assertEqual(series.metadata.completion_mode.value, "evidence")

        # Provenance: canonical ID distinct from source symbol; source label preserved.
        for candle in series.candles:
            self.assertEqual(candle.symbol, "FOREXCOM:EURUSD")
            self.assertEqual(candle.source, "tradingview-mcp:FOREXCOM")
            self.assertEqual(candle.timeframe, "H1")
            self.assertNotEqual(candle.symbol, _CANONICAL.canonical_id)
            self.assertIsInstance(candle.open, Decimal)
            self.assertIsInstance(candle.close, Decimal)

        # Exact call sequence and arguments: health, initial chart state,
        # OHLCV, post-fetch chart state.
        self.assertEqual(
            client.call_order,
            [
                "tv_health_check",
                "chart_get_state",
                "data_get_ohlcv",
                "chart_get_state",
            ],
        )
        self.assertEqual(client.tv_health_check_calls, 1)
        self.assertEqual(client.chart_get_state_calls, 2)
        self.assertEqual(client.data_get_ohlcv_calls, 1)
        self.assertEqual(client.last_arguments("data_get_ohlcv"), {"count": 2})

    def test_only_permitted_tools_are_called(self):
        client = _healthy_client()
        adapter = TvMarketDataSource(_CANONICAL, _BINDING, client, now=_NOW)
        adapter.get_candles(_request())
        for name, _ in client.calls:
            self.assertIn(name, {"tv_health_check", "chart_get_state", "data_get_ohlcv"})


class TvAdapterChartVerificationTests(unittest.TestCase):
    def _mismatch_client(self, symbol, resolution):
        return FakeMcpClient(
            health_response={"success": True},
            chart_states={"success": True, "symbol": symbol, "resolution": resolution},
            ohlcv_response={"success": True, "bars": []},
        )

    def test_wrong_symbol_rejects_before_ohlcv(self):
        client = self._mismatch_client("FOREXCOM:GBPUSD", "60")
        adapter = TvMarketDataSource(_CANONICAL, _BINDING, client, now=_NOW)
        with self.assertRaises(SourcePreconditionError):
            adapter.get_candles(_request())
        self.assertEqual(client.chart_get_state_calls, 1)
        self.assertEqual(client.data_get_ohlcv_calls, 0)

    def test_wrong_timeframe_rejects_before_ohlcv(self):
        client = self._mismatch_client("FOREXCOM:EURUSD", "5")
        adapter = TvMarketDataSource(_CANONICAL, _BINDING, client, now=_NOW)
        with self.assertRaises(SourcePreconditionError):
            adapter.get_candles(_request(timeframe=Timeframe.H1))
        self.assertEqual(client.data_get_ohlcv_calls, 0)

    def test_unsupported_resolution_rejects_before_ohlcv(self):
        client = FakeMcpClient(
            health_response={"success": True},
            chart_states={"success": True, "symbol": "FOREXCOM:EURUSD", "resolution": "15"},
            ohlcv_response={"success": True, "bars": []},
        )
        adapter = TvMarketDataSource(_CANONICAL, _BINDING, client, now=_NOW)
        with self.assertRaises(SourcePreconditionError):
            adapter.get_candles(_request())
        self.assertEqual(client.data_get_ohlcv_calls, 0)

    def test_missing_symbol_rejects_before_ohlcv(self):
        client = FakeMcpClient(
            health_response={"success": True},
            chart_states={"success": True, "resolution": "60"},
            ohlcv_response={"success": True, "bars": []},
        )
        adapter = TvMarketDataSource(_CANONICAL, _BINDING, client, now=_NOW)
        with self.assertRaises(SourcePreconditionError):
            adapter.get_candles(_request())
        self.assertEqual(client.data_get_ohlcv_calls, 0)

    def test_missing_resolution_rejects_before_ohlcv(self):
        client = FakeMcpClient(
            health_response={"success": True},
            chart_states={"success": True, "symbol": "FOREXCOM:EURUSD"},
            ohlcv_response={"success": True, "bars": []},
        )
        adapter = TvMarketDataSource(_CANONICAL, _BINDING, client, now=_NOW)
        with self.assertRaises(SourcePreconditionError):
            adapter.get_candles(_request())
        self.assertEqual(client.data_get_ohlcv_calls, 0)


class TvAdapterAvailabilityTests(unittest.TestCase):
    def test_source_unavailable_propagates(self):
        client = FakeMcpClient(health_error=RuntimeError("CDP connection failed"))
        adapter = TvMarketDataSource(_CANONICAL, _BINDING, client, now=_NOW)
        with self.assertRaises(__import__(
            "trading_automation.data.market_data.contract",
            fromlist=["SourceUnavailableError"],
        ).SourceUnavailableError):
            adapter.get_candles(_request())
        self.assertEqual(client.data_get_ohlcv_calls, 0)

    def test_protocol_failure_propagates(self):
        client = FakeMcpClient(
            health_response={"success": True},
            chart_error=RuntimeError("JSON-RPC parse failure"),
        )
        adapter = TvMarketDataSource(_CANONICAL, _BINDING, client, now=_NOW)
        with self.assertRaises(RuntimeError):
            adapter.get_candles(_request())
        self.assertEqual(client.data_get_ohlcv_calls, 0)


class TvAdapterCountValidationTests(unittest.TestCase):
    def setUp(self):
        self.client = _healthy_client()
        self.adapter = TvMarketDataSource(_CANONICAL, _BINDING, self.client, now=_NOW)

    def test_count_zero_rejected_before_ohlcv(self):
        # CandleRequest itself rejects count < 1 and > 500, so no OHLCV call occurs.
        with self.assertRaises(ValueError):
            CandleRequest(instrument=_CANONICAL, timeframe=Timeframe.H1, count=0)
        self.assertEqual(self.client.data_get_ohlcv_calls, 0)

    def test_count_above_500_rejected_before_ohlcv(self):
        with self.assertRaises(ValueError):
            CandleRequest(instrument=_CANONICAL, timeframe=Timeframe.H1, count=501)
        self.assertEqual(self.client.data_get_ohlcv_calls, 0)


class TvAdapterTimestampTests(unittest.TestCase):
    def _bars(self, times):
        return {
            "success": True,
            "bars": [
                {
                    "time": t,
                    "open": "1.08",
                    "high": "1.09",
                    "low": "1.07",
                    "close": "1.08",
                    "volume": "100",
                }
                for t in times
            ],
        }

    def test_seconds_timestamp_converts(self):
        client = FakeMcpClient(
            health_response={"success": True},
            chart_states={"success": True, "symbol": "FOREXCOM:EURUSD", "resolution": "60"},
            ohlcv_response=self._bars([1731076800]),
        )
        adapter = TvMarketDataSource(_CANONICAL, _BINDING, client, now=_NOW)
        series = adapter.get_candles(_request(count=1))
        expected = to_new_york(datetime.fromtimestamp(1731076800, tz=timezone.utc))
        self.assertEqual(series.candles[0].timestamp, expected)

    def test_milliseconds_timestamp_converts(self):
        client = FakeMcpClient(
            health_response={"success": True},
            chart_states={"success": True, "symbol": "FOREXCOM:EURUSD", "resolution": "60"},
            ohlcv_response=self._bars([1731076800000]),
        )
        adapter = TvMarketDataSource(_CANONICAL, _BINDING, client, now=_NOW)
        series = adapter.get_candles(_request(count=1))
        expected = to_new_york(datetime.fromtimestamp(1731076800, tz=timezone.utc))
        self.assertEqual(series.candles[0].timestamp, expected)

    def test_out_of_range_timestamp_rejected(self):
        client = FakeMcpClient(
            health_response={"success": True},
            chart_states={"success": True, "symbol": "FOREXCOM:EURUSD", "resolution": "60"},
            ohlcv_response=self._bars([99999999999999]),
        )
        adapter = TvMarketDataSource(_CANONICAL, _BINDING, client, now=_NOW)
        with self.assertRaises(__import__(
            "trading_automation.data.market_data.contract",
            fromlist=["MarketDataValidationError"],
        ).MarketDataValidationError):
            adapter.get_candles(_request(count=1))


class TvAdapterValidationTests(unittest.TestCase):
    def _ohlcv(self, bar_overrides=None):
        base = {
            "time": 1731076800,
            "open": "1.08",
            "high": "1.09",
            "low": "1.07",
            "close": "1.08",
            "volume": "100",
        }
        if bar_overrides:
            base.update(bar_overrides)
        return {
            "success": True,
            "bars": [base],
        }

    def _client_with(self, ohlcv_response):
        return FakeMcpClient(
            health_response={"success": True},
            chart_states={"success": True, "symbol": "FOREXCOM:EURUSD", "resolution": "60"},
            ohlcv_response=ohlcv_response,
        )

    def test_missing_required_field_rejected(self):
        client = self._client_with(self._ohlcv({"open": None}))
        adapter = TvMarketDataSource(_CANONICAL, _BINDING, client, now=_NOW)
        with self.assertRaises(__import__(
            "trading_automation.data.market_data.contract",
            fromlist=["MarketDataValidationError"],
        ).MarketDataValidationError):
            adapter.get_candles(_request(count=1))

    def test_non_finite_price_rejected(self):
        client = self._client_with(self._ohlcv({"high": "NaN"}))
        adapter = TvMarketDataSource(_CANONICAL, _BINDING, client, now=_NOW)
        with self.assertRaises(__import__(
            "trading_automation.data.market_data.contract",
            fromlist=["MarketDataValidationError"],
        ).MarketDataValidationError):
            adapter.get_candles(_request(count=1))

    def test_non_positive_price_rejected(self):
        client = self._client_with(self._ohlcv({"low": "0"}))
        adapter = TvMarketDataSource(_CANONICAL, _BINDING, client, now=_NOW)
        with self.assertRaises(__import__(
            "trading_automation.data.market_data.contract",
            fromlist=["MarketDataValidationError"],
        ).MarketDataValidationError):
            adapter.get_candles(_request(count=1))

    def test_invalid_ohlc_relationship_rejected(self):
        client = self._client_with(self._ohlcv({"high": "1.00", "low": "1.07"}))
        adapter = TvMarketDataSource(_CANONICAL, _BINDING, client, now=_NOW)
        # The Candle model enforces OHLC relationships (existing contract).
        with self.assertRaises(ValueError):
            adapter.get_candles(_request(count=1))

    def test_invalid_timestamp_rejected(self):
        client = self._client_with(self._ohlcv({"time": "not-a-number"}))
        adapter = TvMarketDataSource(_CANONICAL, _BINDING, client, now=_NOW)
        with self.assertRaises(__import__(
            "trading_automation.data.market_data.contract",
            fromlist=["MarketDataValidationError"],
        ).MarketDataValidationError):
            adapter.get_candles(_request(count=1))


class TvAdapterCompletionTests(unittest.TestCase):
    def test_latest_candle_remains_incomplete(self):
        client = FakeMcpClient(
            health_response={"success": True},
            chart_states={"success": True, "symbol": "FOREXCOM:EURUSD", "resolution": "60"},
            ohlcv_response={
                "success": True,
                "bars": [
                    {
                        "time": 1731076800,
                        "open": "1.08",
                        "high": "1.09",
                        "low": "1.07",
                        "close": "1.08",
                        "volume": "100",
                    },
                    {
                        "time": 1731080400,
                        "open": "1.082",
                        "high": "1.086",
                        "low": "1.081",
                        "close": "1.084",
                        "volume": "1200",
                    },
                ],
            },
        )
        adapter = TvMarketDataSource(_CANONICAL, _BINDING, client, now=_NOW)
        series = adapter.get_candles(_request(count=2))
        self.assertEqual(len(series.candles), 2)
        # Earlier candle completed through evidence; latest remains incomplete.
        self.assertTrue(series.candles[0].is_closed)
        self.assertFalse(series.candles[1].is_closed)


class TvAdapterBindingTests(unittest.TestCase):
    def test_unverified_binding_rejected(self):
        unverified = SourceBinding(
            adapter="tradingview",
            source_symbol="FOREXCOM:EURUSD",
            source_label="tradingview-mcp:FOREXCOM",
            status="CANDIDATE",
        )
        client = _healthy_client()
        # Unverified binding is rejected at adapter construction.
        with self.assertRaises(InstrumentNotMappedError):
            TvMarketDataSource(_CANONICAL, unverified, client, now=_NOW)

    def test_wrong_adapter_binding_rejected(self):
        wrong = SourceBinding(
            adapter="csv",
            source_symbol="EURUSD",
            source_label="csv:EURUSD",
            status="VERIFIED",
        )
        client = _healthy_client()
        with self.assertRaises(ValueError):
            TvMarketDataSource(_CANONICAL, wrong, client, now=_NOW)

    def test_canonical_identity_remains_distinct(self):
        client = _healthy_client()
        adapter = TvMarketDataSource(_CANONICAL, _BINDING, client, now=_NOW)
        series = adapter.get_candles(_request())
        self.assertEqual(series.instrument.canonical_id, "EURUSD")
        for candle in series.candles:
            self.assertEqual(candle.symbol, "FOREXCOM:EURUSD")
            self.assertEqual(candle.source, "tradingview-mcp:FOREXCOM")


class FakeMcpServerScript:
    """A minimal fake MCP server written to a temp file for integration tests.

    Implements initialize, tools/list, and tools/call for chart_get_state and
    data_get_ohlcv.  Never touches TradingView or CDP.
    """

    _SCRIPT = r'''
import json
import sys

TOOLS = [
    {"name": "tv_health_check", "description": "health", "inputSchema": {"type": "object"}},
    {"name": "chart_get_state", "description": "state", "inputSchema": {"type": "object"}},
    {"name": "data_get_ohlcv", "description": "bars", "inputSchema": {"type": "object"}},
]


def send(msg):
    sys.stdout.write(json.dumps(msg, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def result(request_id, payload):
    send({"jsonrpc": "2.0", "id": request_id, "result": payload})


def tool_error(request_id, message):
    send({
        "jsonrpc": "2.0",
        "id": request_id,
        "result": {
            "content": [{"type": "text", "text": json.dumps({"success": False, "error": message})}],
            "isError": True,
        },
    })


def tool_result(request_id, payload):
    send({
        "jsonrpc": "2.0",
        "id": request_id,
        "result": {
            "content": [{"type": "text", "text": json.dumps(payload)}],
        },
    })


for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        msg = json.loads(line)
    except json.JSONDecodeError:
        continue
    method = msg.get("method")
    req_id = msg.get("id")
    params = msg.get("params") or {}

    if method == "initialize":
        result(req_id, {
            "protocolVersion": "2025-11-25",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "tradingview", "version": "2.0.0"},
        })
    elif method == "notifications/initialized":
        pass
    elif method == "tools/list":
        result(req_id, {"tools": TOOLS})
    elif method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        if name == "chart_get_state":
            tool_result(req_id, {
                "success": True,
                "symbol": "FOREXCOM:EURUSD",
                "resolution": "60",
            })
        elif name == "data_get_ohlcv":
            count = args.get("count", 100)
            tool_result(req_id, {
                "success": True,
                "bars": [
                    {
                        "time": 1731076800,
                        "open": "1.0800",
                        "high": "1.0850",
                        "low": "1.0790",
                        "close": "1.0820",
                        "volume": "1000",
                    }
                ],
                "total_bars": 1,
                "source": "direct_bars",
                "count": count,
            })
        elif name == "tv_health_check":
            tool_result(req_id, {"success": True})
        else:
            tool_error(req_id, f"unknown tool {name}")
    else:
        send({"jsonrpc": "2.0", "id": req_id,
              "error": {"code": -32601, "message": f"unknown method {method}"}})
'''

    def __init__(self) -> None:
        self._tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", delete=False, encoding="utf-8"
        )
        self._tmp.write(self._SCRIPT)
        self._tmp.close()
        self.path = self._tmp.name

    def cleanup(self) -> None:
        try:
            os.unlink(self.path)
        except OSError:
            pass


class TvAdapterRealClientIntegrationTests(unittest.TestCase):
    """Integration-style test using the real McpStdioClient against a fake server.

    This verifies the adapter works through the actual transport abstraction,
    including JSON-RPC serialization and allowlist enforcement, with no
    connection to TradingView or CDP.
    """

    def setUp(self):
        self._server = FakeMcpServerScript()
        self.addCleanup(self._server.cleanup)
        self.addCleanup(self._stop_client)

        self._client = McpStdioClient(
            command=(sys.executable, self._server.path),
            cwd=Path(__file__).resolve().parents[1],
            environment={"FAKE_MCP_MODE": "normal"},
            startup_timeout=15,
            catalog_timeout=15,
            call_timeout=15,
            shutdown_timeout=5,
        )
        self._client.start()
        self._client.initialize()

    def _stop_client(self):
        try:
            self._client.close()
        except Exception:
            pass

    def test_real_client_receives_valid_series(self):
        adapter = TvMarketDataSource(_CANONICAL, _BINDING, self._client, now=_NOW)
        series = adapter.get_candles(_request(count=1))

        self.assertEqual(len(series.candles), 1)
        candle = series.candles[0]
        self.assertEqual(candle.symbol, "FOREXCOM:EURUSD")
        self.assertEqual(candle.source, "tradingview-mcp:FOREXCOM")
        self.assertEqual(candle.timeframe, "H1")
        self.assertEqual(candle.open, Decimal("1.0800"))
        self.assertEqual(candle.close, Decimal("1.0820"))
        # Latest candle remains incomplete (evidence completion).
        self.assertFalse(candle.is_closed)
        self.assertEqual(series.metadata.adapter_name, "tradingview")


class TvAdapterClockLifecycleTests(unittest.TestCase):
    """The fetch-time clock must be resolved per request, not frozen at
    construction."""

    def test_fetch_time_resolved_per_request_not_frozen_at_construction(self):
        T0 = datetime(2026, 11, 8, 10, 0, 0, tzinfo=timezone.utc)
        T1 = datetime(2026, 11, 8, 11, 0, 0, tzinfo=timezone.utc)

        # Explicit now=T0 → fetched_at_utc is T0 (not some other time).
        client_a = _healthy_client()
        adapter_a = TvMarketDataSource(_CANONICAL, _BINDING, client_a, now=T0)
        series_a = adapter_a.get_candles(_request(count=1))
        self.assertEqual(series_a.fetched_at_utc, T0)

        # Explicit now=T1 → fetched_at_utc is T1, proving the time is
        # resolved per request from the now parameter, not frozen globally.
        client_b = _healthy_client()
        adapter_b = TvMarketDataSource(_CANONICAL, _BINDING, client_b, now=T1)
        series_b = adapter_b.get_candles(_request(count=1))
        self.assertEqual(series_b.fetched_at_utc, T1)

    def test_future_candle_still_rejected(self):
        T0 = datetime(2026, 11, 8, 10, 0, 0, tzinfo=timezone.utc)
        future_time = datetime(2026, 11, 8, 12, 0, 0, tzinfo=timezone.utc)

        client = FakeMcpClient(
            health_response={"success": True},
            chart_states=[
                {"success": True, "symbol": "FOREXCOM:EURUSD", "resolution": "60"},
                {"success": True, "symbol": "FOREXCOM:EURUSD", "resolution": "60"},
            ],
            ohlcv_response={
                "success": True,
                "bars": [{
                    "time": int(future_time.timestamp()),
                    "open": "1.08",
                    "high": "1.09",
                    "low": "1.07",
                    "close": "1.08",
                    "volume": "100",
                }],
            },
        )
        adapter = TvMarketDataSource(_CANONICAL, _BINDING, client, now=T0)
        with self.assertRaises(__import__(
            "trading_automation.data.market_data.contract",
            fromlist=["MarketDataValidationError"],
        ).MarketDataValidationError) as ctx:
            adapter.get_candles(_request(count=1))
        self.assertIn("FUTURE_CANDLE", str(ctx.exception))


class TvAdapterPostFetchChartVerificationTests(unittest.TestCase):
    """The chart must be verified again after OHLCV retrieval."""

    def _two_state_client(self, first_state, second_state):
        return FakeMcpClient(
            health_response={"success": True},
            chart_states=[first_state, second_state],
            ohlcv_response={
                "success": True,
                "bars": [{
                    "time": 1731076800,
                    "open": "1.08",
                    "high": "1.09",
                    "low": "1.07",
                    "close": "1.08",
                    "volume": "100",
                }],
            },
        )

    def test_symbol_changes_between_reads_rejected(self):
        client = self._two_state_client(
            {"success": True, "symbol": "FOREXCOM:EURUSD", "resolution": "60"},
            {"success": True, "symbol": "FOREXCOM:GBPUSD", "resolution": "60"},
        )
        adapter = TvMarketDataSource(_CANONICAL, _BINDING, client, now=_NOW)
        with self.assertRaises(SourcePreconditionError):
            adapter.get_candles(_request())
        # Both chart_get_state calls happen; no OHLCV data is returned.
        self.assertEqual(client.chart_get_state_calls, 2)
        self.assertEqual(client.data_get_ohlcv_calls, 1)

    def test_resolution_changes_between_reads_rejected(self):
        client = self._two_state_client(
            {"success": True, "symbol": "FOREXCOM:EURUSD", "resolution": "60"},
            {"success": True, "symbol": "FOREXCOM:EURUSD", "resolution": "5"},
        )
        adapter = TvMarketDataSource(_CANONICAL, _BINDING, client, now=_NOW)
        with self.assertRaises(SourcePreconditionError):
            adapter.get_candles(_request())
        self.assertEqual(client.chart_get_state_calls, 2)
        self.assertEqual(client.data_get_ohlcv_calls, 1)


class TvAdapterPayloadHashTests(unittest.TestCase):
    """The raw OHLCV MCP payload is hashed in memory; nothing is written to
    disk."""

    def _ohlcv_payload(self):
        return {
            "success": True,
            "bars": [{
                "time": 1731076800,
                "open": "1.0800",
                "high": "1.0850",
                "low": "1.0790",
                "close": "1.0820",
                "volume": "1000",
            }],
            "total_bars": 1,
            "source": "direct_bars",
        }

    def _client_with_payload(self, payload):
        return FakeMcpClient(
            health_response={"success": True},
            chart_states=[
                {"success": True, "symbol": "FOREXCOM:EURUSD", "resolution": "60"},
                {"success": True, "symbol": "FOREXCOM:EURUSD", "resolution": "60"},
            ],
            ohlcv_response=payload,
        )

    def test_payload_sha256_and_byte_count(self):
        payload = self._ohlcv_payload()
        raw_text = json.dumps(
            payload, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        expected_sha = hashlib.sha256(raw_text).hexdigest()

        client = self._client_with_payload(payload)
        adapter = TvMarketDataSource(_CANONICAL, _BINDING, client, now=_NOW)
        series = adapter.get_candles(_request(count=1))

        self.assertEqual(series.raw_payload_sha256, expected_sha)
        self.assertEqual(series.raw_payload_bytes, len(raw_text))

    def test_identical_payloads_produce_identical_hashes(self):
        payload = self._ohlcv_payload()
        client_a = self._client_with_payload(payload)
        client_b = self._client_with_payload(dict(payload))
        adapter_a = TvMarketDataSource(_CANONICAL, _BINDING, client_a, now=_NOW)
        adapter_b = TvMarketDataSource(_CANONICAL, _BINDING, client_b, now=_NOW)
        series_a = adapter_a.get_candles(_request(count=1))
        series_b = adapter_b.get_candles(_request(count=1))
        self.assertEqual(series_a.raw_payload_sha256, series_b.raw_payload_sha256)

    def test_different_payloads_produce_different_hashes(self):
        payload_a = self._ohlcv_payload()
        payload_b = dict(self._ohlcv_payload())
        payload_b["bars"][0]["volume"] = "2000"
        client_a = self._client_with_payload(payload_a)
        client_b = self._client_with_payload(payload_b)
        adapter_a = TvMarketDataSource(_CANONICAL, _BINDING, client_a, now=_NOW)
        adapter_b = TvMarketDataSource(_CANONICAL, _BINDING, client_b, now=_NOW)
        series_a = adapter_a.get_candles(_request(count=1))
        series_b = adapter_b.get_candles(_request(count=1))
        self.assertNotEqual(series_a.raw_payload_sha256, series_b.raw_payload_sha256)

    def test_raw_data_not_written_to_disk(self):
        payload = self._ohlcv_payload()
        client = self._client_with_payload(payload)
        adapter = TvMarketDataSource(_CANONICAL, _BINDING, client, now=_NOW)

        tmpdir = tempfile.mkdtemp(prefix="tv_hash_test_")
        files_before = set(os.listdir(tmpdir))
        try:
            series = adapter.get_candles(_request(count=1))
            files_after = set(os.listdir(tmpdir))
            self.assertEqual(files_before, files_after)
            self.assertIsNotNone(series.raw_payload_sha256)
            self.assertIsNotNone(series.raw_payload_bytes)
        finally:
            for name in os.listdir(tmpdir):
                os.unlink(os.path.join(tmpdir, name))
            os.rmdir(tmpdir)


class TvAdapterTimestampEdgeCaseTests(unittest.TestCase):
    """Timestamp parsing must reject booleans, NaN, infinity, and fractional
    truncation, while preserving exact microsecond conversion."""

    def _client_with_time(self, time_value):
        return FakeMcpClient(
            health_response={"success": True},
            chart_states=[
                {"success": True, "symbol": "FOREXCOM:EURUSD", "resolution": "60"},
                {"success": True, "symbol": "FOREXCOM:EURUSD", "resolution": "60"},
            ],
            ohlcv_response={
                "success": True,
                "bars": [{
                    "time": time_value,
                    "open": "1.08",
                    "high": "1.09",
                    "low": "1.07",
                    "close": "1.08",
                    "volume": "100",
                }],
            },
        )

    def test_fractional_seconds_preserved(self):
        # 1731076800.5 seconds = 1731076800 seconds + 500000 microseconds.
        client = self._client_with_time(1731076800.5)
        adapter = TvMarketDataSource(_CANONICAL, _BINDING, client, now=_NOW)
        series = adapter.get_candles(_request(count=1))
        expected = to_new_york(
            datetime.fromtimestamp(1731076800, tz=timezone.utc)
        ) + __import__("datetime").timedelta(microseconds=500000)
        self.assertEqual(series.candles[0].timestamp, expected)

    def test_fractional_milliseconds_preserved(self):
        # 1731076800500.5 milliseconds = 1731076800 seconds + 500500 microseconds.
        client = self._client_with_time(1731076800500.5)
        adapter = TvMarketDataSource(_CANONICAL, _BINDING, client, now=_NOW)
        series = adapter.get_candles(_request(count=1))
        expected = to_new_york(
            datetime.fromtimestamp(1731076800, tz=timezone.utc)
        ) + __import__("datetime").timedelta(microseconds=500500)
        self.assertEqual(series.candles[0].timestamp, expected)

    def test_boolean_timestamp_rejected(self):
        client = self._client_with_time(True)
        adapter = TvMarketDataSource(_CANONICAL, _BINDING, client, now=_NOW)
        with self.assertRaises(__import__(
            "trading_automation.data.market_data.contract",
            fromlist=["MarketDataValidationError"],
        ).MarketDataValidationError):
            adapter.get_candles(_request(count=1))

    def test_nan_timestamp_rejected(self):
        client = self._client_with_time(float("nan"))
        adapter = TvMarketDataSource(_CANONICAL, _BINDING, client, now=_NOW)
        with self.assertRaises(__import__(
            "trading_automation.data.market_data.contract",
            fromlist=["MarketDataValidationError"],
        ).MarketDataValidationError):
            adapter.get_candles(_request(count=1))

    def test_infinity_timestamp_rejected(self):
        client = self._client_with_time(float("inf"))
        adapter = TvMarketDataSource(_CANONICAL, _BINDING, client, now=_NOW)
        with self.assertRaises(__import__(
            "trading_automation.data.market_data.contract",
            fromlist=["MarketDataValidationError"],
        ).MarketDataValidationError):
            adapter.get_candles(_request(count=1))

    def test_negative_infinity_timestamp_rejected(self):
        client = self._client_with_time(float("-inf"))
        adapter = TvMarketDataSource(_CANONICAL, _BINDING, client, now=_NOW)
        with self.assertRaises(__import__(
            "trading_automation.data.market_data.contract",
            fromlist=["MarketDataValidationError"],
        ).MarketDataValidationError):
            adapter.get_candles(_request(count=1))


class TvAdapterMcpErrorTests(unittest.TestCase):
    """MCP tool errors must be mapped to the appropriate market-data error
    category, not silently converted into missing-field errors."""

    def test_chart_state_tool_error(self):
        client = FakeMcpClient(
            health_response={"success": True},
            chart_states=[
                {
                    "success": False,
                    "error": "chart not available",
                },
                {
                    "success": False,
                    "error": "chart not available",
                },
            ],
            ohlcv_response={"success": True, "bars": []},
        )
        adapter = TvMarketDataSource(_CANONICAL, _BINDING, client, now=_NOW)
        with self.assertRaises(SourcePreconditionError) as ctx:
            adapter.get_candles(_request())
        self.assertIn("chart not available", str(ctx.exception))
        self.assertEqual(client.data_get_ohlcv_calls, 0)

    def test_ohlcv_tool_error_false_success(self):
        client = FakeMcpClient(
            health_response={"success": True},
            chart_states=[
                {"success": True, "symbol": "FOREXCOM:EURUSD", "resolution": "60"},
                {"success": True, "symbol": "FOREXCOM:EURUSD", "resolution": "60"},
            ],
            ohlcv_response={
                "success": False,
                "error": "CDP read timeout",
            },
        )
        adapter = TvMarketDataSource(_CANONICAL, _BINDING, client, now=_NOW)
        with self.assertRaises(SourceUnavailableError) as ctx:
            adapter.get_candles(_request())
        self.assertIn("CDP read timeout", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
