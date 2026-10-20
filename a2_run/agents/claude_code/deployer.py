"""ClaudeCodeDeployer — drives the @anthropic-ai/claude-code CLI.

Claude-specific parts only: npm install, provider env routing (direct /
bedrock / vertex, ADC + CA-bundle materialization), argv construction, and
Anthropic stream-json parsing. Lifecycle lives in BlockingCliDeployer.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import ClassVar

from a2_run.base_interface import (
    ContentPart,
    ImageSource,
    Observation,
    StepMetrics,
    ToolCall,
    ToolResult,
    TrajectoryBuilder,
)

from a2_run.agents._blocking_cli import (
    BlockingCliDeployer,
    SegmentTracker,
    _read_events,
)
from a2_run.agents._bootstrap import ensure_pinned_npm_cli

from .config import ClaudeCodeConfig

logger = logging.getLogger(__name__)


# Ceilings for the CLI's blocking MCP a2a_send_message: 24h wall, 2h idle
# (the poll keeps the connection active well under this).
_MCP_TOOL_TIMEOUT_MS = 24 * 60 * 60 * 1000
_MCP_TOOL_IDLE_TIMEOUT_MS = 2 * 60 * 60 * 1000


class ClaudeCodeDeployer(BlockingCliDeployer):
    """Stdlib-only deployer for the @anthropic-ai/claude-code CLI."""

    harness: ClassVar[str] = "claude_code"
    standing_context_filename: ClassVar[str] = "CLAUDE.md"
    pid_stem: ClassVar[str] = "claude"
    session_id_filename: ClassVar[str] = "session_id.txt"
    session_id_mode: ClassVar[str] = "host"

    @property
    def version(self) -> str | None:
        cfg: ClaudeCodeConfig = self.config  # type: ignore[assignment]
        return cfg.cli_version

    async def install(self) -> None:
        cfg: ClaudeCodeConfig = self.config  # type: ignore[assignment]
        self._claude_path, _ = await ensure_pinned_npm_cli(
            command="claude",
            npm_spec=cfg.cli_version,
            default_package="@anthropic-ai/claude-code",
            harness=self.harness,
        )
        if cfg.provider == "vertex":
            self._write_gcp_adc()
        self._ca_bundle_path = self._write_ca_bundle()
        Path(self.executor.work_dir).mkdir(parents=True, exist_ok=True)

    def _write_gcp_adc(self) -> None:
        # Missing ADC is fatal now, not minutes into the run when the first
        # model call fails with an opaque auth error.
        content = (self.executor.env or {}).get("GCP_ADC_JSON") or os.environ.get(
            "GCP_ADC_JSON", ""
        )
        adc_path = Path(os.path.expanduser("~")) / ".config" / "gcloud" / \
            "application_default_credentials.json"
        if not content.strip():
            if not adc_path.exists():
                raise RuntimeError(
                    "claude_code: provider=vertex but no GCP_ADC_JSON received "
                    f"and no ADC at {adc_path} — vertex auth cannot succeed. "
                    "Check env_passthrough shipped the host ADC "
                    "(see orchestration/unit_common._materialise_gcp_adc)."
                )
            logger.info(
                "claude_code: no GCP_ADC_JSON shipped; using ADC already at %s",
                adc_path,
            )
            return
        adc_path.parent.mkdir(parents=True, exist_ok=True)
        adc_path.write_text(content, encoding="utf-8")
        try:
            adc_path.chmod(0o600)
        except OSError as e:
            # Non-POSIX fs; single-tenant container — tolerable, but say so.
            logger.warning("claude_code: could not chmod 0600 %s: %s", adc_path, e)
        logger.info("claude_code: ADC written to %s", adc_path)

    def _write_ca_bundle(self) -> str | None:
        # Host CA bundle (public certs) so node TLS trusts a privately-signed
        # gateway; the host NODE_EXTRA_CA_CERTS path would dangle in-container.
        content = (self.executor.env or {}).get("A2_CA_BUNDLE_PEM") or os.environ.get(
            "A2_CA_BUNDLE_PEM", ""
        )
        if not content.strip():
            return None
        # Under the per-run work_dir (relocated to the control plane at
        # /opt/a2a/sessions, out of the agent's home) so it does not surface a
        # framework-owned ``~/.sessions`` to an agent enumerating its home.
        ca_path = Path(self.executor.work_dir) / "ca-bundle.pem"
        ca_path.parent.mkdir(parents=True, exist_ok=True)
        ca_path.write_text(content, encoding="utf-8")
        try:
            ca_path.chmod(0o644)
        except OSError as e:
            logger.warning("claude_code: could not chmod 0644 %s: %s", ca_path, e)
        logger.info("claude_code: CA bundle written to %s (%d B)", ca_path, len(content))
        return str(ca_path)

    def _build_turn_argv(
        self, *, session_id: str | None, resume: bool,
    ) -> list[str]:
        cfg: ClaudeCodeConfig = self.config  # type: ignore[assignment]
        argv = [
            self._claude_path,
            "-p", "-",
            # No --bare: discovering ./CLAUDE.md as project memory IS the
            # identity mechanism.
            "--output-format", "stream-json", "--verbose",
            "--model", cfg.model,
        ]
        # Not --fork-session: forking branches into a new id and orphans the
        # thread the next resume expects.
        if session_id:
            argv += ["--resume", session_id] if resume else ["--session-id", session_id]
        if cfg.effort_level:
            argv += ["--effort", cfg.effort_level]
        if cfg.max_turns is not None and cfg.max_turns >= 0:
            argv += ["--max-turns", str(cfg.max_turns)]
        if cfg.max_budget_usd is not None:
            argv += ["--max-budget-usd", str(cfg.max_budget_usd)]
        if cfg.dangerously_skip_permissions:
            argv += ["--dangerously-skip-permissions"]
        for tool in cfg.disabled_tools:
            argv += ["--disallowedTools", tool]
        # Rewritten every turn so a resume sees the current roster env.
        mcp_config = Path(self.executor.work_dir) / "mcp_config.json"
        mcp_config.write_text(
            json.dumps(self._a2a_mcp_servers_dict(), indent=2), encoding="utf-8",
        )
        argv += ["--mcp-config", str(mcp_config), "--strict-mcp-config"]
        return argv

    def _build_env(self) -> dict[str, str]:
        cfg: ClaudeCodeConfig = self.config  # type: ignore[assignment]
        env = super()._build_env()
        # ADC/CA content landed on disk in install(); don't ship the blobs.
        env.pop("GCP_ADC_JSON", None)
        env.pop("A2_CA_BUNDLE_PEM", None)
        ca_path = getattr(self, "_ca_bundle_path", None)
        if ca_path:
            env["NODE_EXTRA_CA_CERTS"] = ca_path
        else:
            env.pop("NODE_EXTRA_CA_CERTS", None)

        # Reasoning controls — three mutually exclusive modes:
        #   effort_level set        → --effort only; MAX_THINKING_TOKENS too
        #                             would 400 on Opus 4.7/4.8 (budget_tokens).
        #   max_thinking_tokens set → pin the extended-thinking budget.
        #   max_thinking_tokens None→ CLAUDE_CODE_DISABLE_THINKING=1 omits the
        #                             thinking param entirely — required for
        #                             adaptive-thinking models (fable-5) over
        #                             OpenRouter, whose gateway mangles
        #                             thinking={type:"adaptive"} into a 400;
        #                             the model still thinks natively.
        if cfg.effort_level:
            env.pop("MAX_THINKING_TOKENS", None)
            env.pop("CLAUDE_CODE_DISABLE_THINKING", None)
        elif cfg.max_thinking_tokens is not None:
            env["MAX_THINKING_TOKENS"] = str(cfg.max_thinking_tokens)
            env.pop("CLAUDE_CODE_DISABLE_THINKING", None)
        else:
            env.pop("MAX_THINKING_TOKENS", None)
            env["CLAUDE_CODE_DISABLE_THINKING"] = "1"

        # Env-provided values win so an operator can still tune the ceilings.
        env.setdefault("MCP_TOOL_TIMEOUT", str(_MCP_TOOL_TIMEOUT_MS))
        env.setdefault("CLAUDE_CODE_MCP_TOOL_IDLE_TIMEOUT",
                       str(_MCP_TOOL_IDLE_TIMEOUT_MS))

        # Provider-driven routing (explicit, not key-presence heuristic).
        if cfg.provider == "direct":
            key = cfg.api_key or env.get("ANTHROPIC_API_KEY")
            if not key:
                raise RuntimeError(
                    "claude_code: provider=direct but neither config api_key "
                    "nor ANTHROPIC_API_KEY is set"
                )
            env["ANTHROPIC_API_KEY"] = key
            if cfg.base_url:
                env["ANTHROPIC_BASE_URL"] = cfg.base_url
        elif cfg.provider == "openrouter":
            # OpenRouter's Anthropic-compatible "skin": the CLI's Anthropic SDK
            # talks to ANTHROPIC_BASE_URL/v1/messages, and OpenRouter (a gateway)
            # authenticates with a Bearer token via ANTHROPIC_AUTH_TOKEN — NOT
            # the x-api-key ANTHROPIC_API_KEY carries. ANTHROPIC_API_KEY must be
            # blanked so the SDK does not send a conflicting x-api-key header.
            # This lets the claude_code harness drive ANY OpenRouter model
            # (e.g. openai/gpt-5, google/gemini-2.5-pro) under model="vendor/model".
            key = (
                cfg.api_key
                or env.get("OPENROUTER_API_KEY")
                or env.get("ANTHROPIC_AUTH_TOKEN")
            )
            if not key:
                raise RuntimeError(
                    "claude_code: provider=openrouter but none of config api_key, "
                    "OPENROUTER_API_KEY, or ANTHROPIC_AUTH_TOKEN is set"
                )
            env["ANTHROPIC_AUTH_TOKEN"] = key
            env["ANTHROPIC_BASE_URL"] = cfg.base_url or "https://openrouter.ai/api"
            env.pop("ANTHROPIC_API_KEY", None)
            env.pop("OPENROUTER_API_KEY", None)
        elif cfg.provider == "bedrock":
            env["CLAUDE_CODE_USE_BEDROCK"] = "1"
        elif cfg.provider == "vertex":
            env["CLAUDE_CODE_USE_VERTEX"] = "1"
            # Long legal drafting turns (30k+ output tokens) exceeded the CLI's
            # default per-request timeout on Vertex ("Request timed out" ->
            # terminal_reason=api_error) in ~40% of legal/practice_suite runs. 30 minutes.
            env.setdefault("API_TIMEOUT_MS", "1800000")
            # The dominant failure was the CLI's stream watchdog ("Response
            # stalled mid-stream" -> api_error) during the single long
            # generation that writes the memo. Allow 15 minutes of silence.
            env.setdefault("CLAUDE_STREAM_IDLE_TIMEOUT_MS", "900000")
        else:
            raise RuntimeError(
                f"claude_code: unknown provider {cfg.provider!r} "
                "(expected 'direct', 'openrouter', 'bedrock', or 'vertex')"
            )
        return env

    @staticmethod
    def _extract_turn_outputs(transcript: Path) -> tuple[str | None, str | None]:
        result_text: str | None = None
        session_id: str | None = None
        for event in _read_events(transcript, harness="claude_code"):
            etype = event.get("type")
            if etype == "system" and event.get("subtype") == "init":
                session_id = event.get("session_id") or session_id
            elif etype == "result":
                session_id = event.get("session_id") or session_id
                res = event.get("result")
                if isinstance(res, str) and res:
                    result_text = res
        return result_text, session_id

    def _classify_failure(self, stderr_text: str, tx_text: str) -> list[str]:
        parts: list[str] = []
        if '"authentication_failed"' in tx_text or '"User not found"' in tx_text:
            parts.append("LLM auth failed (check api keys)")
        elif '"error_status":429' in tx_text or '"rate_limit_error"' in tx_text:
            parts.append("LLM rate-limited")
        elif '"error_status":5' in tx_text:
            parts.append("LLM upstream 5xx")
        elif '"type":"result"' not in tx_text:
            parts.append("agent never produced result event")
        return parts

    # ---- trajectory replay (host-side) ----

    #: Tool-name suffix marking an outbound peer message (a turn boundary).
    _SEND_TOOL_SUFFIX: ClassVar[str] = "a2a_send_message"

    @classmethod
    def _replay_blocking_transcript(
        cls,
        transcript_file: Path,
        builder: TrajectoryBuilder,
        incomings: list[str],
    ) -> None:
        events = cls._load_session_events(transcript_file, builder)
        segments = SegmentTracker(builder, incomings)
        for event in events:
            etype = event.get("type")
            if etype == "assistant":
                cls._consume_assistant(event, builder)
                content = (event.get("message", {}) or {}).get("content") or []
                for block in content:
                    if (block.get("type") == "tool_use" and str(
                            block.get("name") or "").endswith(cls._SEND_TOOL_SUFFIX)):
                        segments.advance(drop_id=block.get("id") or None)
            elif etype == "user":
                cls._consume_user(event, builder, drop_tool_ids=segments.drop_ids)
            else:
                cls._consume_event(event, builder)

    @classmethod
    def _sum_usage(
        cls, events: list[dict],
    ) -> tuple[int, int, int, int, float | None]:
        ti = to = tcr = tcc = 0
        tcost = 0.0
        have_cost = False
        for event in events:
            u = event.get("usage") or {}
            ti += u.get("input_tokens") or 0
            to += u.get("output_tokens") or 0
            tcr += u.get("cache_read_input_tokens") or 0
            tcc += u.get("cache_creation_input_tokens") or 0
            c = event.get("total_cost_usd")
            if c is not None:
                tcost += c
                have_cost = True
        return ti, to, tcr, tcc, (tcost if have_cost else None)

    @classmethod
    def _consume_event(cls, event: dict, builder: TrajectoryBuilder) -> None:
        etype = event.get("type")
        if etype == "assistant":
            cls._consume_assistant(event, builder)
        elif etype == "user":
            cls._consume_user(event, builder)
        elif etype == "system":
            builder.trajectory.extra.setdefault("system_events", []).append(event)
        elif etype == "result":
            # Authoritative per-invocation usage + cost; append so
            # apply_authoritative_metrics can sum a resumed role's extras.
            builder.trajectory.extra.setdefault("results", []).append(event)

    @staticmethod
    def _consume_assistant(event: dict, builder: TrajectoryBuilder) -> None:
        message = event.get("message", {}) or {}
        text_parts: list[str] = []
        thinking_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        for block in message.get("content", []) or []:
            btype = block.get("type")
            if btype == "text":
                text_parts.append(block.get("text", ""))
            elif btype == "thinking":
                thinking_parts.append(block.get("thinking", ""))
            elif btype == "tool_use":
                tool_calls.append(ToolCall(
                    id=block.get("id") or "",
                    name=block.get("name") or "",
                    arguments=block.get("input") or {},
                ))
        usage = message.get("usage") or {}
        metrics = StepMetrics(
            input_tokens=usage.get("input_tokens"),
            output_tokens=usage.get("output_tokens"),
            cache_read_tokens=usage.get("cache_read_input_tokens"),
            cache_creation_tokens=usage.get("cache_creation_input_tokens"),
        )
        builder.add_step(
            source="agent",
            message="\n".join(p for p in text_parts if p) or None,
            reasoning="\n".join(p for p in thinking_parts if p) or None,
            tool_calls=tool_calls,
            metrics=metrics,
            extra={"stop_reason": message.get("stop_reason")},
        )

    @staticmethod
    def _consume_user(
        event: dict,
        builder: TrajectoryBuilder,
        drop_tool_ids: set[str] | None = None,
    ) -> None:
        message = event.get("message", {}) or {}
        results: list[ToolResult] = []
        text_parts: list[str] = []
        for block in message.get("content", []) or []:
            btype = block.get("type")
            if btype == "tool_result":
                # A send's result is the peer reply — already the next turn's
                # incoming; suppress to avoid duplication.
                if drop_tool_ids and block.get("tool_use_id") in drop_tool_ids:
                    continue
                content = block.get("content")
                parts: list[ContentPart] = []
                if isinstance(content, str):
                    parts.append(ContentPart(type="text", text=content))
                elif isinstance(content, list):
                    for c in content:
                        if not isinstance(c, dict):
                            continue
                        ctype = c.get("type")
                        if ctype == "text":
                            parts.append(ContentPart(type="text", text=c.get("text", "")))
                        elif ctype == "image":
                            # Keep inline so persist_screenshots writes them to
                            # screenshots/ and rewrites to path refs.
                            src = c.get("source") or {}
                            if src.get("type") == "base64" and src.get("data"):
                                parts.append(ContentPart(
                                    type="image",
                                    image=ImageSource(
                                        type="base64",
                                        media_type=src.get("media_type", "image/png"),
                                        data=src.get("data"),
                                    ),
                                ))
                            elif src.get("type") == "url" and src.get("url"):
                                parts.append(ContentPart(
                                    type="image",
                                    image=ImageSource(type="url", url=src.get("url")),
                                ))
                results.append(ToolResult(
                    tool_call_id=block.get("tool_use_id") or "",
                    content=parts,
                    is_error=bool(block.get("is_error")),
                ))
            elif btype == "text":
                text_parts.append(block.get("text", ""))
        text = "\n".join(p for p in text_parts if p) or None
        # A user event carrying ONLY a dropped send result has nothing left.
        if drop_tool_ids and not results and not text:
            return
        builder.add_step(
            source="environment",
            message=text,
            observation=Observation(results=results),
        )
