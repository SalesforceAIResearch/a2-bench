"""CursorConfig: per-episode knobs for the Cursor agent CLI deployer.

Auth: CURSOR_API_KEY (crsr_...) from the operator env. No BYOK/OpenRouter —
cursor-agent only talks to Cursor's own backend. Model ids are Cursor
catalog names (``cursor-agent --list-models``), reasoning tier encoded in
the id itself (…-low / -medium / -high).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar


@dataclass
class CursorConfig:
    """Tunables for :class:`CursorDeployer`."""

    name: ClassVar[str] = "cursor"

    model: str = ""
    """Cursor catalog id. Empty = "auto" (no --model). Benchmark runs should
    pin one so the run directory names the model that actually ran."""

    provider: str = "cursor"
    """Fixed routing marker (no BYOK path); exists for parity with the other
    harness configs. The deployer does not branch on it."""

    cursor_version: str = "2026.07.09-c59fd9a"
    """Pinned cursor-agent version, installed from downloads.cursor.com; a
    failed pinned download is a hard error (no latest fallback). Bumping
    REQUIRES re-verifying CursorDeployer._patch_mcp_timeout against the new
    bundle — cursor-agent hard-codes the MCP SDK's 60s request timeout with
    no knob, which breaks blocking a2a sends; install() fails loudly if the
    patch pattern no longer matches."""

    disabled_tools: tuple[str, ...] = ()
    """Permission deny patterns for ~/.cursor/cli-config.json, e.g.
    "Shell(rm *)" or "Mcp(server, tool)". Empty: the outer benchmark
    container is the real sandbox."""

    allow_web_tools: bool = False
