"""CodexDeployer — drives the OpenAI Codex CLI (``codex exec --json``).

Codex-specific parts only: config.toml MCP registration, argv/env
construction, thread/item event parsing. Lifecycle lives in
BlockingCliDeployer. Plain final output is not an addressed message and is
never used as the wire response.
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

from .config import CodexConfig

logger = logging.getLogger(__name__)


def _toml_string(value: str) -> str:
    """A JSON string is also a valid TOML basic string."""
    return json.dumps(str(value), ensure_ascii=True)


def _arguments(value: object) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {"raw": value}
        return parsed if isinstance(parsed, dict) else {"value": parsed}
    return {}


def _tool_name(item: dict, started: dict | None = None) -> str:
    started = started or {}
    return str(
        item.get("tool")
        or item.get("name")
        or started.get("tool")
        or started.get("name")
        or ""
    )


def _is_send_message(item: dict, started: dict | None = None) -> bool:
    name = _tool_name(item, started)
    return name == "a2a_send_message" or name.endswith("__a2a_send_message")


class CodexDeployer(BlockingCliDeployer):
    """Run the stock OpenAI Codex CLI inside the benchmark sandbox."""

    harness: ClassVar[str] = "codex"
    standing_context_filename: ClassVar[str] = "AGENTS.md"
    pid_stem: ClassVar[str] = "codex"
    session_id_filename: ClassVar[str] = "codex_session_id.txt"
    session_id_mode: ClassVar[str] = "cli"
    usage_events_key: ClassVar[str] = "codex_turn_usage"
    cost_unknown_when_missing: ClassVar[bool] = True  # tokens, no price

    @property
    def version(self) -> str | None:
        cfg: CodexConfig = self.config  # type: ignore[assignment]
        return cfg.cli_version

    async def install(self) -> None:
        cfg: CodexConfig = self.config  # type: ignore[assignment]
        self._codex_path, _ = await ensure_pinned_npm_cli(
            command="codex",
            npm_spec=cfg.cli_version,
            default_package="@openai/codex",
            harness=self.harness,
        )
        Path(self.executor.work_dir).mkdir(parents=True, exist_ok=True)
        self._write_codex_config(cfg)

    def _write_codex_config(self, cfg: CodexConfig) -> None:
        # Codex loads MCP servers from config.toml, so this happens at
        # install() rather than via launch arguments.
        if cfg.web_search not in {"disabled", "cached", "indexed", "live"}:
            raise ValueError(
                f"codex: invalid web_search mode {cfg.web_search!r}; expected "
                "one of: disabled, cached, indexed, live"
            )
        server, tool_env = self._a2a_server_and_env()

        codex_home = Path(os.path.expanduser("~")) / ".codex"
        codex_home.mkdir(parents=True, exist_ok=True)
        self._codex_home = str(codex_home)

        lines = [
            f"model_reasoning_effort = {_toml_string(cfg.reasoning_effort)}",
            f"sandbox_mode = {_toml_string(cfg.sandbox_mode)}",
            'approval_policy = "never"',
            f"web_search = {_toml_string(cfg.web_search)}",
            "",
            "[features]",
            "multi_agent = false",
            "multi_agent_v2 = false",
            "",
            "[mcp_servers.a2a]",
            f"command = {_toml_string(self.executor.sandbox.python)}",
            f"args = [{_toml_string(str(server))}]",
            "required = true",
            "startup_timeout_sec = 30",
            f"tool_timeout_sec = {int(cfg.mcp_tool_timeout_s)}",
            'default_tools_approval_mode = "approve"',
            "",
            "[mcp_servers.a2a.env]",
        ]
        lines.extend(f"{key} = {_toml_string(value)}" for key, value in tool_env.items())
        (codex_home / "config.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")
        logger.info("codex: configured a2a MCP server at %s", server)

    def _build_turn_argv(
        self, *, session_id: str | None, resume: bool,
    ) -> list[str]:
        cfg: CodexConfig = self.config  # type: ignore[assignment]
        argv = [self._codex_path, "exec"]
        if session_id:
            argv.append("resume")
        argv += ["--model", cfg.model, "--json"]
        if cfg.yolo:
            argv.append("--dangerously-bypass-approvals-and-sandbox")
        else:
            argv += ["--sandbox", cfg.sandbox_mode]
        # Don't mutate the agent's project just to satisfy the repo guard.
        argv.append("--skip-git-repo-check")
        if session_id:
            argv.append(session_id)
        # Explicit '-' makes stdin the complete initial/resume prompt.
        argv.append("-")
        return argv

    def _build_env(self) -> dict[str, str]:
        env = super()._build_env()
        api_key = env.get("OPENAI_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError(
                "codex: OPENAI_API_KEY is required; add it to the experiment "
                "secret file or operator environment"
            )
        env["CODEX_API_KEY"] = api_key
        env["CODEX_HOME"] = self._codex_home
        return env

    @staticmethod
    def _extract_turn_outputs(transcript: Path) -> tuple[str | None, str | None]:
        final_text: str | None = None
        session_id: str | None = None
        for event in _read_events(transcript, harness="codex"):
            if event.get("type") == "thread.started":
                session_id = event.get("thread_id") or event.get("threadId")
            if event.get("type") == "item.completed":
                item = event.get("item") or {}
                if item.get("type") == "agent_message" and item.get("text"):
                    final_text = str(item["text"])
        return final_text, session_id

    def _classify_failure(self, stderr_text: str, tx_text: str) -> list[str]:
        lowered = (stderr_text + tx_text).lower()
        parts: list[str] = []
        if ("unauthorized" in lowered or "invalid api key" in lowered
                or "401" in lowered):
            parts.append("LLM auth failed (check OPENAI_API_KEY)")
        elif "rate limit" in lowered or "429" in lowered:
            parts.append("LLM rate-limited")
        return parts

    # ---- trajectory replay (host-side) ----

    @classmethod
    def _replay_blocking_transcript(
        cls,
        transcript: Path,
        builder: TrajectoryBuilder,
        incomings: list[str],
    ) -> None:
        events = cls._load_session_events(transcript, builder)
        # Codex user steps historically carry no turn_index extra.
        segments = SegmentTracker(builder, incomings, tag_turn_index=False)

        started_items: dict[str, dict] = {}
        completed_ids: set[str] = set()

        for event in events:
            event_type = event.get("type", "")
            if event_type == "thread.started":
                builder.trajectory.extra.setdefault("codex", {})["thread_id"] = (
                    event.get("thread_id") or event.get("threadId")
                )
                continue
            if event_type == "item.started":
                item = event.get("item") or {}
                item_id = str(item.get("id") or "")
                if item_id:
                    started_items[item_id] = item
                continue
            if event_type == "item.completed":
                item = event.get("item") or {}
                item_id = str(item.get("id") or "")
                started = started_items.get(item_id, {})
                is_send = _is_send_message(item, started)
                cls._consume_item_completed(
                    event,
                    started_items,
                    completed_ids,
                    builder,
                    suppress_result=is_send,
                )
                if is_send:
                    segments.advance()
                continue
            if event_type == "turn.completed":
                cls._consume_turn_completed(event, builder)
                continue
            if event_type in {"turn.failed", "error"}:
                builder.add_step(
                    source="system",
                    message=str(event.get("message") or event.get("error") or event),
                )

        for item_id, item in started_items.items():
            if item_id in completed_ids or item.get("type") != "mcp_tool_call":
                continue
            builder.add_step(
                source="agent",
                tool_calls=[ToolCall(
                    id=item_id,
                    name=_tool_name(item),
                    arguments=_arguments(item.get("arguments")),
                )],
                extra={"server": item.get("server", ""), "status": "incomplete"},
            )

        builder.trajectory.extra.setdefault("codex", {})["transcript_path"] = str(
            transcript
        )

    @classmethod
    def _consume_item_completed(
        cls,
        event: dict,
        started_items: dict[str, dict],
        completed_ids: set[str],
        builder: TrajectoryBuilder,
        *,
        suppress_result: bool = False,
    ) -> None:
        item = event.get("item") or {}
        item_type = item.get("type", "")
        item_id = str(item.get("id") or "")
        completed_ids.add(item_id)
        started = started_items.get(item_id, {})

        if item_type == "agent_message":
            builder.add_step(
                source="agent",
                message=str(item.get("text") or ""),
                extra={"item_id": item_id},
            )
            return

        if item_type == "reasoning":
            builder.add_step(
                source="agent",
                reasoning=str(item.get("text") or ""),
                extra={"item_id": item_id},
            )
            return

        if item_type == "command_execution":
            command = item.get("command") or started.get("command") or ""
            output = item.get("aggregated_output") or started.get("aggregated_output") or ""
            builder.add_step(
                source="agent",
                tool_calls=[ToolCall(
                    id=item_id,
                    name="shell",
                    arguments={"command": command},
                )],
            )
            builder.add_step(
                source="environment",
                observation=Observation(results=[ToolResult(
                    tool_call_id=item_id,
                    content=[ContentPart(type="text", text=str(output))],
                    is_error=(item.get("exit_code") or 0) != 0,
                )]),
                extra={"exit_code": item.get("exit_code"), "status": item.get("status")},
            )
            return

        if item_type == "mcp_tool_call":
            tool_name = _tool_name(item, started)
            arguments = _arguments(item.get("arguments") or started.get("arguments"))
            builder.add_step(
                source="agent",
                tool_calls=[ToolCall(id=item_id, name=tool_name, arguments=arguments)],
                extra={"server": item.get("server") or started.get("server"),
                       "status": item.get("status")},
            )
            if suppress_result:
                return
            result_parts, is_error = _result_parts(item)
            if result_parts or item.get("status") == "completed" or is_error:
                builder.add_step(
                    source="environment",
                    observation=Observation(results=[ToolResult(
                        tool_call_id=item_id,
                        content=result_parts,
                        is_error=is_error,
                    )]),
                )
            return

        if item_type == "file_change":
            builder.add_step(
                source="environment",
                extra={"item_id": item_id, "changes": item.get("changes", []),
                       "status": item.get("status")},
            )
            return

        if item_type == "web_search":
            builder.add_step(
                source="agent",
                tool_calls=[ToolCall(
                    id=item_id,
                    name="web_search",
                    arguments={"query": item.get("query", "")},
                )],
            )
            return

        if item_type == "error":
            builder.add_step(source="system", message=str(item.get("message") or ""))

    @staticmethod
    def _consume_turn_completed(event: dict, builder: TrajectoryBuilder) -> None:
        usage = event.get("usage") or {}
        if not usage:
            return
        builder.trajectory.extra.setdefault("codex_turn_usage", []).append(usage)

    @classmethod
    def _sum_usage(
        cls, events: list[dict],
    ) -> tuple[int, int, int, int, float | None]:
        total_input = sum(int(u.get("input_tokens") or 0) for u in events)
        total_output = sum(int(u.get("output_tokens") or 0) for u in events)
        total_cached = sum(int(u.get("cached_input_tokens") or 0) for u in events)
        return total_input, total_output, total_cached, 0, None


def _result_parts(item: dict) -> tuple[list[ContentPart], bool]:
    error = item.get("error")
    if error:
        return [ContentPart(type="text", text=str(error))], True
    result = item.get("result")
    if not result:
        return [], False
    if not isinstance(result, dict):
        return [ContentPart(type="text", text=str(result)[:2000])], False

    content = result.get("content") or []
    parts: list[ContentPart] = []
    for block in content if isinstance(content, list) else []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            parts.append(ContentPart(type="text", text=str(block.get("text") or "")))
        elif block.get("type") == "image" and block.get("data"):
            parts.append(ContentPart(
                type="image",
                image=ImageSource(
                    type="base64",
                    media_type=block.get("mimeType", "image/png"),
                    data=block.get("data"),
                ),
            ))
    if not parts:
        parts.append(ContentPart(type="text", text=json.dumps(result)[:2000]))
    return parts, bool(result.get("isError"))
