"""In-memory fake MCP client for TradingView adapter tests.

This fake implements the subset of the ``McpStdioClient`` interface used by
``TvMarketDataSource``: ``health()`` and ``call_tool(name, arguments)``.  It
never opens a socket, never connects to TradingView or CDP, and never
launches TradingView.

The fake records every call so tests can assert exact tool names, arguments,
call counts, and call order.  Calling an unexpected tool raises an
AssertionError to prove the adapter never invokes forbidden tools.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Mapping


class FakeMcpClient:
    """Programmable fake MCP client capturing all calls."""

    def __init__(
        self,
        *,
        health_response: Mapping[str, Any] | None = None,
        health_error: BaseException | None = None,
        chart_states: Sequence[Mapping[str, Any]] | Mapping[str, Any] | None = None,
        chart_error: BaseException | None = None,
        ohlcv_response: Mapping[str, Any] | None = None,
        ohlcv_error: BaseException | None = None,
        unexpected_tool_error: type[BaseException] | None = AssertionError,
    ) -> None:
        self._health_response = health_response
        self._health_error = health_error
        # Normalize chart_states into an ordered sequence.  A single mapping is
        # wrapped so the same client can be reused across tests that expect one
        # or more chart_get_state responses.
        if chart_states is None:
            self._chart_states: list[Mapping[str, Any]] = []
        elif isinstance(chart_states, Mapping):
            self._chart_states = [chart_states]
        else:
            self._chart_states = list(chart_states)
        self._chart_index = 0
        self._chart_error = chart_error
        self._ohlcv_response = ohlcv_response
        self._ohlcv_error = ohlcv_error
        self._unexpected_tool_error = unexpected_tool_error

        self.calls: list[tuple[str, dict[str, Any]]] = []

    # ------------------------------------------------------------------ call log

    @property
    def chart_get_state_calls(self) -> int:
        return sum(1 for name, _ in self.calls if name == "chart_get_state")

    @property
    def data_get_ohlcv_calls(self) -> int:
        return sum(1 for name, _ in self.calls if name == "data_get_ohlcv")

    @property
    def tv_health_check_calls(self) -> int:
        return sum(1 for name, _ in self.calls if name == "tv_health_check")

    @property
    def call_order(self) -> list[str]:
        return [name for name, _ in self.calls]

    def last_arguments(self, tool_name: str) -> dict[str, Any] | None:
        for name, args in reversed(self.calls):
            if name == tool_name:
                return args
        return None

    # ------------------------------------------------------------------ interface

    def health(self) -> Mapping[str, Any]:
        self.calls.append(("tv_health_check", {}))
        if self._health_error is not None:
            raise self._health_error
        if self._health_response is None:
            return {"success": True}
        return dict(self._health_response)

    def call_tool(self, name: str, arguments: Mapping[str, Any] | None = None) -> Mapping[str, Any]:
        args = dict(arguments or {})
        self.calls.append((name, args))

        if name == "chart_get_state":
            if self._chart_error is not None:
                raise self._chart_error
            if not self._chart_states:
                raise AssertionError("chart_get_state response not programmed")
            if self._chart_index >= len(self._chart_states):
                # Repeat the last programmed state for any additional reads.
                state = self._chart_states[-1]
            else:
                state = self._chart_states[self._chart_index]
                self._chart_index += 1
            return dict(state)

        if name == "data_get_ohlcv":
            if self._ohlcv_error is not None:
                raise self._ohlcv_error
            if self._ohlcv_response is None:
                raise AssertionError("data_get_ohlcv response not programmed")
            return dict(self._ohlcv_response)

        if self._unexpected_tool_error is not None:
            raise self._unexpected_tool_error(
                f"FakeMcpClient: unexpected tool called: {name!r}"
            )
        return {}
