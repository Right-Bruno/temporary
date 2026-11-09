# TradingView MCP Integration (Stage 2)

## Scope

Stage 2 connects OpenCode V2 to the existing pinned TradingView MCP checkout
and proves, without touching a chart, that:

1. the project configuration starts the pinned local server,
2. the live tool catalog matches the approved 84-tool manifest exactly,
3. every agent resolves to deny-by-default, and
4. the temporary diagnostic agent can reach exactly three read-only tools.

Stage 2 does **not** retrieve candles, inspect chart state, change chart state,
write SQLite, retain raw payloads, or submit orders. `chart_get_state` and
`data_get_ohlcv` are permitted for the diagnostic agent but must not be called
until the owner authorises Stage 3. Live execution remains disabled.

## Pinned source

| Item | Value |
| --- | --- |
| Repository | `https://github.com/Right-Bruno/tradingview-mcp.git` |
| Commit | `5afdf7d0ee389909bc15dd1c7806796e3ae1b8e7` |
| Branch used for integration | `opencode-v2-integration` |
| Entrypoint | `src/server.js` |
| Server name / version | `tradingview` / `2.0.0` |
| Transport | stdio (newline-delimited JSON-RPC 2.0) |

The checkout is reused in place at the absolute path recorded in
`opencode.jsonc`; it is never cloned, fetched, reset, or modified by project
code. The manifest pins the version the server declares during `initialize`
(`2.0.0`), which is independent of the package version in its own
`package.json`.

## Tool manifest

`config/mcp-tools.json` is the policy artifact for the pinned commit. It
records, for all 84 tools:

- the MCP tool name and its OpenCode name (`tradingview_<tool>`),
- the description exposed by the server,
- the risk classification (`read_only` or `side_effecting`) and reason,
- the Phase 1 access decision (`allowed` or `denied`),
- the source file that registers the tool.

Totals:

| Classification | Count |
| --- | --- |
| Total tools | 84 |
| Read-only | 31 |
| Side-effecting | 53 |
| Allowed in Phase 1 | 3 |
| Denied in Phase 1 | 81 |

The three allowed tools are exactly:

- `tv_health_check`
- `chart_get_state`
- `data_get_ohlcv`

`src/trading_automation/mcp/manifest.py` rejects any manifest edit that
changes the commit, server identity, classification counts, risk criterion,
allowlist, tool names, or source-file shape. The loader also rejects duplicate
JSON keys, unknown fields, non-finite numbers, and oversized files.

## OpenCode configuration

`opencode.jsonc` defines one local MCP server:

- command: `node src/server.js`
- working directory: the pinned checkout (absolute path; verified against the
  manifest commit rather than trusted)
- environment: `TV_CDP_HOST=127.0.0.1`, `TV_CDP_PORT=9222` only
- `codemode: false`, so native `tradingview_<tool>` names map one-to-one to
  permission actions
- timeouts: startup `30000 ms`, catalog `30000 ms`, execution `60000 ms`

Top-level permissions contain exactly three deny rules:

```jsonc
{ "action": "tradingview_*", "resource": "*", "effect": "deny" }
{ "action": "opencode_list_mcp_resources", "resource": "tradingview", "effect": "deny" }
{ "action": "opencode_read_mcp_resource", "resource": "tradingview:*", "effect": "deny" }
```

Because the first rule matches every TradingView tool and OpenCode resolves
permissions last-match-wins, no agent can use the server unless it appends a
narrower allow. The second and third rules also deny MCP resource discovery and
reads for this server.

The permission matcher in `src/trading_automation/mcp/config.py` implements the
same whole-value `*`/`?` wildcard and last-match-wins semantics used by
OpenCode. It is validated against the real `opencode debug agents` output.

## Diagnostic agent

`.opencode/agents/tv-mcp-diagnostic.md` is a temporary `subagent` created under
decision D-11. Its frontmatter starts with a deny-all rule and appends exactly
three allows:

```yaml
permissions:
  - action: "*"
    resource: "*"
    effect: deny
  - action: tradingview_tv_health_check
    resource: "*"
    effect: allow
  - action: tradingview_chart_get_state
    resource: "*"
    effect: allow
  - action: tradingview_data_get_ohlcv
    resource: "*"
    effect: allow
```

The deny-all entry point also means the agent has no shell, edit, or subagent
access. `src/trading_automation/mcp/config.py` verifies this shape and simulates
the effective rule set for all 84 tools; `build` and every other agent resolve
to 84 denies, while `tv-mcp-diagnostic` resolves to exactly three allows.

Stage 2 uses only `tv_health_check`. The health probe may report that
TradingView is closed; it must not launch or reconfigure TradingView.

## Checks and exit codes

```powershell
.\.venv\Scripts\python.exe -m trading_automation check-mcp
.\.venv\Scripts\python.exe -m trading_automation check-mcp --health
.\.venv\Scripts\python.exe -m trading_automation check-mcp --json
```

`check-mcp` runs, in order:

1. **manifest** - load and validate `config/mcp-tools.json`.
2. **opencode_config** - parse and validate `opencode.jsonc` (JSONC, server
   command, absolute working directory, environment, timeouts, top-level
   policy).
3. **permission_policy** - parse the diagnostic agent, verify its rules, and
   simulate the effective effect for every tool.
4. **source_integrity** - confirm the checkout HEAD, clean tracked tree, origin
   repository, entrypoint, and all 84 manifest source files.
5. **handshake** - start the server, perform `initialize`, and verify the
   declared name and version.
6. **catalog** - paginate `tools/list` and require an exact match with the
   manifest, including count and duplicate checks.
7. **health** - skipped unless `--health` is given; when requested, calls only
   `tv_health_check`.

Exit codes:

| Code | Meaning |
| --- | --- |
| `0` | Catalog, source pin, and permission policy valid (health passed if requested) |
| `20` | MCP source or TradingView/CDP unavailable, or handshake/health timed out |
| `21` | Reserved for chart-precondition checks (Stage 3) |
| `22` | Reserved for market-data validation (later stage) |
| `23` | Configuration, manifest, catalog, source-pin, protocol, or policy failure |

With TradingView closed and port 9222 unbound, `check-mcp` exits `0` because it
does not touch CDP, and `check-mcp --health` exits `20` after `tv_health_check`
fails. That `20` is the expected Stage 2 result, not a policy failure.

## Safety boundaries

- No SQLite connection or schema change; no `data/raw/` output.
- No raw MCP tool payload retention. The check report keeps tool names and
  counts, and the health payload only when `--health` succeeds.
- No browser/CDP usage in Stage 2 tests.
- No MT5, broker, order, strategy-rule, Runtime Controller, or AI-agent
  strategy authority.
- Failure to start the server, verify the source, or match the catalog fails
  closed with exit `20` or `23`; it never falls back to a different server,
  commit, or tool set.

## Tests

Run the whole suite with:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Stage 2 coverage:

- `tests/test_mcp_manifest.py` - manifest content and every tamper case.
- `tests/test_mcp_policy.py` - JSONC parsing, config validation, agent rules,
  and permission simulation.
- `tests/test_mcp_client.py` - stdio handshake, pagination, allowlist
  enforcement, protocol errors, oversize handling, and timeouts.
- `tests/test_mcp_check.py` - orchestration and exit-code mapping.
- `tests/test_cli_mcp.py` - CLI arguments, JSON/text output, and reserved exit
  codes.
- `tests/test_real_mcp_handshake.py` - real pinned server catalog and full
  `check-mcp` run without health or CDP access.
- `tests/test_opencode_effective_permissions.py` - real
  `opencode debug agents` output checked against the manifest.
