"""Configuration for the OpenAI Codex CLI a2a deployer.

The only credential this harness accepts is ``OPENAI_API_KEY`` from the
operator environment.  The deployer maps it to Codex's non-interactive
``CODEX_API_KEY`` variable only for the child process; no key is serialized in
the agent config or written to disk.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar


@dataclass
class CodexConfig:
    """Per-run knobs consumed by :class:`CodexDeployer`."""

    name: ClassVar[str] = "codex"

    model: str = "gpt-5.6-luna"
    reasoning_effort: str = "high"

    # The agent already runs inside a disposable benchmark container, so the
    # default avoids interactive approval prompts inside that outer sandbox.
    sandbox_mode: str = "danger-full-access"
    yolo: bool = True

    web_search: str = "disabled"

    # A send_message call remains open while the peer thinks and responds.
    # Keep Codex's MCP deadline above the experiment wall budget.
    mcp_tool_timeout_s: int = 24 * 60 * 60

    cli_version: str = "@openai/codex@0.145.0"
