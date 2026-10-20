"""ClaudeCodeConfig: per-episode knobs for the Claude Code CLI deployer.

**API keys live in the operator's shell env**, never in this config.
The deployer reads ``ANTHROPIC_API_KEY`` from the inherited env
(propagated host → VM by the env passthrough).

Typical usage::

    # In shell:
    #   export ANTHROPIC_API_KEY=sk-ant-...    # direct

    cfg = ClaudeCodeConfig(
        model="claude-opus-4-7",
        max_budget_usd=5.0,
    )
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

# Built-in Claude Code tools that break headless (`-p`) runs. Mirrors the
# default disabled_tools list shipped in agenthle's claude_code_openrouter.yaml
# — passed to the CLI as repeated ``--disallowedTools`` flags. Each either
# blocks on human interaction or mutates persistent session state with no
# headless equivalent, so leaving them enabled risks deadlocks.
_DISABLED_TOOLS = (
    # Plan mode needs interactive user approval (ExitPlanMode waits for a
    # human accept) — deadlocks headless runs.
    "EnterPlanMode",
    "ExitPlanMode",
    # Worktree tools mutate session CWD with persistent side effects.
    "EnterWorktree",
    "ExitWorktree",
    # Pure user-interaction tool — no headless equivalent.
    "AskUserQuestion",
    # Background task lifecycle (no running task ID exists in headless).
    "TaskOutput",
    "TaskStop",
    # Requires a logged-in claude.ai account; benchmark VMs are not.
    "RemoteTrigger",
    "WebSearch",
    "WebFetch",
)


@dataclass
class ClaudeCodeConfig:
    """Tunables for :class:`ClaudeCodeDeployer`.

    Standalone config (no shared base). The episode wall-budget is
    orchestration-owned.
    """

    name: ClassVar[str] = "claude-code"

    model: str = "claude-sonnet-4-6"

    # -1 ≡ unlimited; the deployer omits --max-turns when < 0.
    max_turns: int | None = -1

    # ---- routing (no secrets — API keys come from shell env) ----
    provider: str = "direct"
    """Routing provider, drives env setup explicitly (not key-presence
    heuristics):
      - ``"direct"`` → uses ANTHROPIC_API_KEY against anthropic.com (or
        ``base_url`` if set). Requires ANTHROPIC_API_KEY.
      - ``"openrouter"`` → routes the CLI's Anthropic SDK at OpenRouter's
        Anthropic-compatible endpoint (ANTHROPIC_BASE_URL defaults to
        ``https://openrouter.ai/api``, override with ``base_url``). OpenRouter
        is a gateway: it authenticates with a Bearer token via
        ANTHROPIC_AUTH_TOKEN, so the deployer sets that (from ``api_key`` /
        OPENROUTER_API_KEY / ANTHROPIC_AUTH_TOKEN) and blanks ANTHROPIC_API_KEY.
        Use ``model: "vendor/model"`` (e.g. ``openai/gpt-5``,
        ``google/gemini-2.5-pro``) to drive non-Claude models through the same
        claude_code harness. For adaptive-thinking models over OpenRouter set
        ``max_thinking_tokens: null`` (see that field).
      - ``"bedrock"`` → CLAUDE_CODE_USE_BEDROCK=1; the CLI authenticates via
        ambient AWS credentials (AWS_ACCESS_KEY_ID/..., AWS_REGION or
        AWS_BEARER_TOKEN_BEDROCK) passed through the env.
      - ``"vertex"`` → CLAUDE_CODE_USE_VERTEX=1; the CLI authenticates via
        ambient GCP credentials (ANTHROPIC_VERTEX_PROJECT_ID, CLOUD_ML_REGION,
        application-default credentials) passed through the env.
    Missing the required key for the chosen provider is a hard error."""

    base_url: str | None = None
    """Custom Anthropic-compatible base URL. Overrides the provider's
    default ``ANTHROPIC_BASE_URL``."""

    api_key: str | None = None
    """Literal API key, used in place of the provider's env-var key. ``None`` ⇒
    read ANTHROPIC_API_KEY from the env (provider ``direct``). When set
    (typically ``api_key: ${env:...}`` in the agent yaml, resolved host-side),
    it travels with the serialized config — no env passthrough whitelist change
    needed and no collision with a real shell key."""

    # ---- CLI knobs ----
    max_budget_usd: float | None = None
    disabled_tools: tuple[str, ...] = _DISABLED_TOOLS
    dangerously_skip_permissions: bool = True

    max_thinking_tokens: int | None = 31999
    """Extended-thinking token budget, passed to the CLI via the
    ``MAX_THINKING_TOKENS`` env var (Claude Code's documented knob —
    see https://code.claude.com/docs/en/costs#adjust-extended-thinking).
    Claude Code enables extended thinking by default; ``31999`` is the
    default-high ("ultrathink") cap. We set it explicitly so the reasoning
    level is pinned + visible rather than relying on the CLI default. Lower
    it (e.g. 8000) to cut cost, or set 0 to disable thinking.

    ``null``/``None`` ⇒ the deployer sets ``CLAUDE_CODE_DISABLE_THINKING=1``
    so the CLI OMITS the thinking param from API requests — required for
    adaptive-thinking models (e.g. claude-fable-5) over OpenRouter: the CLI
    otherwise sends ``thinking: {type: "adaptive"}``, which OpenRouter
    mangles into ``thinking: {type: "disabled"}`` upstream, and those
    models reject it with 400 ("thinking.type.disabled is not supported").
    With the param absent the provider applies the model's native adaptive
    thinking — the model still thinks; this is not a thinking-off switch.
    (Merely unsetting MAX_THINKING_TOKENS is NOT enough — the CLI still
    sends adaptive. Verified by request capture + e2e tool flow.)"""

    effort_level: str | None = None
    """Reasoning-effort level for adaptive-thinking models (Claude Opus 4.7/4.8,
    Fable 5, ...), passed to the CLI as ``--effort <level>``. This is the
    Anthropic-native control that maps to the API's ``output_config.effort`` —
    DISTINCT from ``max_thinking_tokens``/``budget_tokens`` (the legacy extended-
    thinking budget, which is removed → 400 on Opus 4.7/4.8). Accepted values:
    ``low | medium | high | xhigh | max`` (verified against the pinned CLI's
    ``--effort <level>`` option). Claude Code only applies effort to supported
    Claude models; it's a no-op on non-Claude endpoints.

    ``None`` ⇒ don't pass ``--effort`` (let the CLI use its default). When SET,
    the deployer ALSO skips ``MAX_THINKING_TOKENS``/``CLAUDE_CODE_DISABLE_THINKING``
    entirely: effort is the reasoning control for these models, and pairing it
    with the legacy budget env is wrong (budget_tokens 400s on 4.7/4.8). Thinking
    is left to the model's adaptive default."""

    # ---- documentation ----
    cli_version: str = "@anthropic-ai/claude-code@2.1.170"
    """Full npm spec of the pinned CLI. The deployer probes the installed
    ``claude --version`` against the version encoded here and (re)installs
    via npm when missing or mismatched, so bumping this is sufficient to
    select the runtime CLI version."""
