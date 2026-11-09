"""Provenance checks for the pinned local TradingView MCP checkout.

The Stage 2 policy authorises one exact commit of one repository.  These
checks prove that the configured working directory is that checkout, that its
tracked working tree is clean, and that every source file named by the
manifest exists inside it.  Nothing here fetches, updates, or writes to git.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from trading_automation.mcp.errors import McpPolicyError, McpUnavailableError
from trading_automation.mcp.manifest import ToolManifest

GIT_TIMEOUT_SECONDS = 15
MAX_GIT_OUTPUT_CHARS = 4_096
_GITHUB_REMOTE_RE = re.compile(
    r"github\.com[:/](?P<slug>[^/\s]+/[^/\s]+?)(?:\.git)?/?$",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class SourceState:
    root: Path
    commit: str
    clean_tracked_tree: bool
    remote_url: str
    repository_slug: str
    entrypoint: str
    checked_source_files: int


def verify_source_checkout(manifest: ToolManifest, root: str | Path) -> SourceState:
    """Verify the checkout at ``root`` against the manifest source pin."""

    checkout = Path(root)
    try:
        resolved_root = checkout.resolve(strict=True)
    except OSError as exc:
        raise McpUnavailableError(
            f"TradingView MCP checkout is unavailable: {checkout}"
        ) from exc
    if not resolved_root.is_dir():
        raise McpUnavailableError(
            f"TradingView MCP checkout is not a directory: {resolved_root}"
        )
    if not (resolved_root / ".git").exists():
        raise McpUnavailableError(
            f"TradingView MCP checkout is not a git working tree: {resolved_root}"
        )

    head = _git_output(resolved_root, ["rev-parse", "HEAD"]).strip().lower()
    if not re.fullmatch(r"[0-9a-f]{40}", head):
        raise McpUnavailableError(
            "Could not read a valid git HEAD from the TradingView MCP checkout"
        )
    if head != manifest.source.commit:
        raise McpPolicyError(
            f"TradingView MCP checkout HEAD is {head}; expected pinned commit "
            f"{manifest.source.commit}"
        )

    status = _git_output(
        resolved_root, ["status", "--porcelain", "--untracked-files=no"]
    )
    if status.strip():
        raise McpPolicyError(
            "TradingView MCP checkout has modified tracked files; "
            "the pinned commit must be clean"
        )

    remote_url = _git_output(
        resolved_root,
        ["config", "--get", "remote.origin.url"],
        allowed_returncodes=(0, 1),
    ).strip()
    if not remote_url:
        raise McpPolicyError(
            "TradingView MCP checkout has no origin remote; provenance cannot "
            "be verified"
        )
    repository_slug = _repository_slug(remote_url)
    expected_slug = _repository_slug(manifest.source.repository)
    if repository_slug != expected_slug:
        raise McpPolicyError(
            f"TradingView MCP origin remote points to {remote_url!r}; expected "
            f"the approved repository {manifest.source.repository!r}"
        )

    entrypoint = resolved_root / manifest.source.entrypoint
    if not entrypoint.is_file():
        raise McpPolicyError(
            f"TradingView MCP entrypoint is missing: {manifest.source.entrypoint}"
        )

    checked = 0
    missing: list[str] = []
    for tool in manifest.tools:
        source_path = resolved_root / tool.source_file
        try:
            source_path.resolve(strict=True).relative_to(resolved_root)
        except (OSError, ValueError):
            missing.append(tool.source_file)
            continue
        if not source_path.is_file():
            missing.append(tool.source_file)
            continue
        checked += 1
    if missing:
        raise McpPolicyError(
            "TradingView MCP checkout is missing manifest source files: "
            + ", ".join(sorted(set(missing)))
        )

    return SourceState(
        root=resolved_root,
        commit=head,
        clean_tracked_tree=True,
        remote_url=remote_url,
        repository_slug=repository_slug,
        entrypoint=manifest.source.entrypoint,
        checked_source_files=checked,
    )


def _git_output(
    root: Path,
    arguments: list[str],
    *,
    allowed_returncodes: tuple[int, ...] = (0,),
) -> str:
    command = ["git", "-C", str(root), *arguments]
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=GIT_TIMEOUT_SECONDS,
            shell=False,
        )
    except FileNotFoundError as exc:
        raise McpUnavailableError("git executable is not available") from exc
    except subprocess.TimeoutExpired as exc:
        raise McpUnavailableError(
            "git command timed out while checking the TradingView MCP checkout"
        ) from exc
    except OSError as exc:
        raise McpUnavailableError(
            f"Could not run git while checking the TradingView MCP checkout: {exc}"
        ) from exc

    if completed.returncode not in allowed_returncodes:
        stderr = (completed.stderr or "").strip()
        if len(stderr) > MAX_GIT_OUTPUT_CHARS:
            stderr = stderr[:MAX_GIT_OUTPUT_CHARS]
        detail = f": {stderr}" if stderr else ""
        raise McpUnavailableError(
            f"git {' '.join(arguments)} failed in the TradingView MCP checkout"
            f" (exit {completed.returncode}){detail}"
        )
    return completed.stdout or ""


def _repository_slug(remote_url: str) -> str:
    match = _GITHUB_REMOTE_RE.search(remote_url.strip())
    if not match:
        raise McpPolicyError(
            f"TradingView MCP remote is not a recognised GitHub URL: {remote_url!r}"
        )
    return match.group("slug").lower()
