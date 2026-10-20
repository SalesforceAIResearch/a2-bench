"""CursorDeployer — drives Anysphere's ``cursor-agent`` CLI for a2a pairs.

Cursor-specific parts only: pinned tarball install + MCP-timeout bundle
patch, headless permission staging, argv construction, and dual-family
transcript parsing (claude-compatible stream-json PLUS Composer-style
``tool_call`` started/completed events). Lifecycle lives in
BlockingCliDeployer. cursor-agent mints its own chat ids, so resume mirrors
codex (turn 0 scrapes, later turns ``--resume``), not claude.

Auth: CURSOR_API_KEY from the process env. Caveat: a bare key cannot
re-authenticate a dropped session — that surfaces as a loud failure.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import platform
import shutil
import subprocess
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

from .config import CursorConfig

logger = logging.getLogger(__name__)

_SEND_TOOL = "a2a_send_message"


def _is_send_name(name: str | None) -> bool:
    # Suffix match: cursor surfaces MCP tools as "<server>-<tool>"
    # ("a2a-a2a_send_message"); the tool name itself doesn't vary.
    return bool(name) and name.endswith(_SEND_TOOL)


class CursorDeployer(BlockingCliDeployer):
    """Stdlib-only deployer for the ``cursor-agent`` CLI."""

    harness: ClassVar[str] = "cursor"
    standing_context_filename: ClassVar[str] = "AGENTS.md"
    pid_stem: ClassVar[str] = "cursor"
    session_id_filename: ClassVar[str] = "cursor_session_id.txt"
    session_id_mode: ClassVar[str] = "cli"
    cost_unknown_when_missing: ClassVar[bool] = True  # tokens, no price

    # A client-side MCP timeout means a blocking send was abandoned and the
    # peer's reply consumed by the dead request — the role converses deaf
    # from then on. _patch_mcp_timeout should make this impossible.
    transport_error_patterns: ClassVar[tuple[str, ...]] = ("MCP error -32001",)
    transport_error_hint: ClassVar[str] = (
        "A blocking send was abandoned client-side and the peer reply lost; "
        "check the MCP timeout patch (_patch_mcp_timeout) for this "
        "cursor_version."
    )

    @property
    def version(self) -> str | None:
        cfg: CursorConfig = self.config  # type: ignore[assignment]
        return cfg.cursor_version or None

    # ---- install ----

    @staticmethod
    def _probe_version(cursor_path: str) -> str:
        """`--version` prints just the version (e.g. "2026.07.09-c59fd9a")."""
        try:
            probe = subprocess.run(
                [cursor_path, "--version"],
                capture_output=True, text=True, timeout=30,
                stdin=subprocess.DEVNULL,
            )
        except (subprocess.TimeoutExpired, OSError):
            return ""
        return (probe.stdout or "").strip().splitlines()[0].strip() if probe.stdout else ""

    @staticmethod
    def _download_url(version: str) -> str:
        machine = platform.machine().lower()
        arch = "arm64" if machine in ("arm64", "aarch64") else "x64"
        osname = "darwin" if platform.system() == "Darwin" else "linux"
        return (
            f"https://downloads.cursor.com/lab/{version}/{osname}/{arch}/"
            "agent-cli-package.tar.gz"
        )

    async def _install_pinned(self, version: str) -> None:
        """Install an exact cursor-agent version (official installer layout).

        A failed pinned download is a hard error — deliberately NO fallback
        to cursor.com/install (latest-only), which would silently drift the
        version and the bundle layout _patch_mcp_timeout depends on.
        """
        home = os.path.expanduser("~")
        version_dir = f"{home}/.local/share/cursor-agent/versions/{version}"
        bin_dir = f"{home}/.local/bin"
        url = self._download_url(version)
        script = (
            f'set -e; mkdir -p "{version_dir}" "{bin_dir}"; '
            f'curl -fSL -s "{url}" | tar --strip-components=1 -xzf - -C "{version_dir}"; '
            f'rm -f "{bin_dir}/cursor-agent"; '
            f'ln -s "{version_dir}/cursor-agent" "{bin_dir}/cursor-agent"'
        )
        proc = await asyncio.to_thread(
            subprocess.run,
            ["bash", "-c", script],
            capture_output=True, text=True, timeout=300,
        )
        if bin_dir not in os.environ.get("PATH", ""):
            os.environ["PATH"] = f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"
        if proc.returncode != 0:
            raise RuntimeError(
                f"cursor: pinned download of {version} failed "
                f"(rc={proc.returncode}) from {url}: {(proc.stderr or '')[:500]}"
            )
        logger.info("cursor: installed pinned version %s from %s", version, url)

    async def install(self) -> None:
        cfg: CursorConfig = self.config  # type: ignore[assignment]
        pinned = cfg.cursor_version

        # Verify-and-correct: keep a pre-installed binary only if it matches
        # the pin; otherwise reinstall over the mismatched copy.
        cursor_path = shutil.which("cursor-agent")
        current = (
            await asyncio.to_thread(self._probe_version, cursor_path)
            if cursor_path else ""
        )
        if cursor_path and (not pinned or current == pinned):
            logger.info(
                "cursor: reusing installed cursor-agent %s (version %s)",
                cursor_path, current or "unknown",
            )
        else:
            if cursor_path:
                logger.info(
                    "cursor: installed version %r != pinned %s — reinstalling",
                    current or "unknown", pinned,
                )
            else:
                logger.info("cursor: 'cursor-agent' not on PATH, installing pinned %s …", pinned)
            await self._install_pinned(pinned)
            cursor_path = shutil.which("cursor-agent")
            if not cursor_path:
                raise RuntimeError(
                    "CursorDeployer: 'cursor-agent' still not found after install"
                )
        self._cursor_path = cursor_path
        self._patch_mcp_timeout(cursor_path)

        Path(self.executor.work_dir).mkdir(parents=True, exist_ok=True)
        cursor_home = Path(os.path.expanduser("~")) / ".cursor"
        cursor_home.mkdir(parents=True, exist_ok=True)

        # a2a MCP server — auto-discovered from ~/.cursor/mcp.json (no flag).
        (cursor_home / "mcp.json").write_text(
            json.dumps(self._a2a_mcp_servers_dict(), indent=2), encoding="utf-8",
        )

        (cursor_home / "cli-config.json").write_text(
            json.dumps(self._cli_config_dict(cfg), indent=2), encoding="utf-8",
        )

        # The CLI's cwd is the home dir; a project-scoped .cursor/cli.json
        # there would shadow cli-config.json.
        project_cli = cursor_home / "cli.json"
        if project_cli.exists():
            project_cli.unlink()

        self._setup_cursor_auth()
        logger.info("cursor: config staged at %s", cursor_home)

    #: The SDK default cursor-agent inherits (60s) and the ceiling we need
    #: (24h, matching the other harnesses). cursor-agent calls callTool with
    #: NO options and exposes no knob, so its bundled default is the only
    #: place to set this.
    _MCP_TIMEOUT_ORIG: ClassVar[str] = "?.timeout??6e4"
    _MCP_TIMEOUT_PATCHED: ClassVar[str] = "?.timeout??864e5"

    def _patch_mcp_timeout(self, cursor_path: str) -> None:
        """Raise cursor-agent's hard-coded 60s MCP request timeout to 24h.

        At 60s the CLI abandons a blocking send with ``MCP error -32001``,
        the peer's reply is eaten by the dead request, and the model loops
        re-greeting. The pinned bundle minifies the default to exactly one
        ``?.timeout??6e4`` literal in index.js; anything else (bundle drift
        after a version bump) is a hard error — a 60s a2a ceiling silently
        breaks every slow-peer conversation.
        """
        bundle_dir = Path(cursor_path).resolve().parent
        index_js = bundle_dir / "index.js"
        if not index_js.is_file():
            raise RuntimeError(
                f"cursor: cannot patch MCP timeout — no index.js beside "
                f"{cursor_path} (resolved bundle dir {bundle_dir})"
            )
        text = index_js.read_text(encoding="utf-8")
        if self._MCP_TIMEOUT_PATCHED in text:
            logger.info("cursor: MCP timeout already patched in %s", index_js)
            return
        count = text.count(self._MCP_TIMEOUT_ORIG)
        if count != 1:
            raise RuntimeError(
                f"cursor: expected exactly 1 occurrence of "
                f"{self._MCP_TIMEOUT_ORIG!r} in {index_js}, found {count} — "
                "the pinned bundle changed; re-verify the timeout patch for "
                "this cursor_version before running a2a conversations"
            )
        index_js.write_text(
            text.replace(self._MCP_TIMEOUT_ORIG, self._MCP_TIMEOUT_PATCHED),
            encoding="utf-8",
        )
        logger.info(
            "cursor: patched MCP request timeout 60s -> 24h in %s", index_js,
        )

    @staticmethod
    def _cli_config_dict(cfg: CursorConfig) -> dict:
        """Headless permission posture for ~/.cursor/cli-config.json.

        The outer benchmark container is the real sandbox; permissions only
        decide what auto-runs. Two mutually exclusive postures (verified
        in-container on the pinned version with a live web-search task):

        - ``allow_web_tools=True`` → "unrestricted" (+ ``--force`` in argv):
          everything runs, including cursor's SERVER-SIDE WebSearch/WebFetch
          (they execute on Cursor's backend over the model connection, so
          the container egress allowlist never sees them).
        - ``allow_web_tools=False`` → "allowlist" WITHOUT ``--force``:
          shell/read/write/MCP auto-run via the allow tokens below, while
          WebSearch/WebFetch — deliberately absent — need an approval prompt
          that headless ``-p`` cannot show, so the CLI auto-rejects them
          ("User Rejected"). This is the ONLY working off switch: under
          ``--force`` the CLI approves web tools without consulting
          ``permissions.deny`` (verified: deny entries for WebSearch/
          WebFetch were ignored and the search ran), and the
          CURSOR_FORCED_SHELL_EGRESS env gate also kills them but forces a
          shell sandbox that cannot start in the container (no user
          namespaces ⇒ every shell command dies "Terminal unavailable").
        """
        allow = [
            "Shell(*)",
            "Read(**)",
            "Write(**)",
            "WebFetch(*)",
            "Mcp(*:*)",
        ]
        if not cfg.allow_web_tools:
            allow.remove("WebFetch(*)")
        return {
            "version": 1,
            "permissions": {
                "allow": allow,
                "deny": list(cfg.disabled_tools),
            },
            "approvalMode": (
                "unrestricted" if cfg.allow_web_tools else "allowlist"
            ),
            "sandbox": {
                "mode": "disabled",
                "networkAccess": "user_config_with_defaults",
            },
        }

    def _setup_cursor_auth(self) -> None:
        # Never log the key, only its byte count.
        api_key = (
            (self.executor.env or {}).get("CURSOR_API_KEY")
            or os.environ.get("CURSOR_API_KEY", "")
        ).strip()
        if not api_key:
            raise RuntimeError(
                "cursor: no credentials — set CURSOR_API_KEY in the "
                "experiment secret file / operator env. cursor-agent has no "
                "BYOK/OpenRouter path."
            )
        logger.info("cursor: using CURSOR_API_KEY from env (%d B)", len(api_key))

    def _build_turn_argv(
        self, *, session_id: str | None, resume: bool,
    ) -> list[str]:
        cfg: CursorConfig = self.config  # type: ignore[assignment]
        argv = [self._cursor_path, "-p"]
        if session_id:
            argv += ["--resume", session_id]
        # Empty model ⇒ omit --model so cursor-agent picks "auto".
        if cfg.model:
            argv += ["--model", cfg.model]
        argv += [
            "--output-format", "stream-json",
            "--approve-mcps", "--trust",
            "--sandbox", "disabled",
        ]
        # --force implies run-everything, which auto-approves the SERVER-SIDE
        # WebSearch/WebFetch tools without consulting permissions.deny. With
        # web tools disabled we rely on allowlist mode instead (see install):
        # everything the benchmark needs is allowlisted, and headless -p
        # auto-rejects the web tools' approval prompt.
        if cfg.allow_web_tools:
            argv.insert(argv.index("--approve-mcps"), "--force")
        return argv

    @staticmethod
    def _extract_turn_outputs(transcript: Path) -> tuple[str | None, str | None]:
        # Cursor's stream-json is claude-compatible but not byte-identical:
        # accept both snake_case and camelCase id fields.
        result_text: str | None = None
        session_id: str | None = None

        def _sid(event: dict) -> str | None:
            for key in ("session_id", "sessionId", "chat_id", "chatId"):
                val = event.get(key)
                if val:
                    return str(val)
            return None

        for event in _read_events(transcript, harness="cursor"):
            etype = event.get("type")
            if etype == "system" and event.get("subtype") == "init":
                session_id = _sid(event) or session_id
            elif etype == "result":
                session_id = _sid(event) or session_id
                res = event.get("result")
                if isinstance(res, str) and res:
                    result_text = res
        return result_text, session_id

    def _classify_failure(self, stderr_text: str, tx_text: str) -> list[str]:
        lowered = (stderr_text + tx_text).lower()
        if ("authentication" in lowered or "logged out" in lowered
                or "unauthorized" in lowered):
            return [
                "cursor auth failed (check CURSOR_API_KEY; a bare key cannot "
                "re-authenticate a dropped session)"
            ]
        return []

    # ---- trajectory replay (host-side) ----

    @classmethod
    def _replay_blocking_transcript(
        cls,
        transcript_file: Path,
        builder: TrajectoryBuilder,
        incomings: list[str],
    ) -> None:
        # Sends surface as assistant tool_use blocks (claude-style) OR as
        # Composer tool_call started events; both are turn boundaries.
        events = cls._load_session_events(transcript_file, builder)
        segments = SegmentTracker(builder, incomings)
        thinking_parts: list[str] = []

        for event in events:
            etype = event.get("type")
            if etype == "assistant":
                cls._consume_assistant(event, builder)
                content = (event.get("message", {}) or {}).get("content") or []
                for block in content:
                    if (block.get("type") == "tool_use"
                            and _is_send_name(block.get("name"))):
                        segments.advance(drop_id=block.get("id") or None)
            elif etype == "user":
                cls._consume_user(event, builder, drop_tool_ids=segments.drop_ids)
            elif etype == "tool_call":
                if cls._consume_tool_call(event, builder, drop_ids=segments.drop_ids):
                    segments.advance()
            elif etype == "thinking":
                # Streamed {"subtype":"delta","text":…} chunks closed by a
                # bare {"subtype":"completed"} marker (no aggregate text).
                if event.get("subtype") == "delta":
                    text = event.get("text")
                    if isinstance(text, str) and text:
                        thinking_parts.append(text)
                elif event.get("subtype") == "completed" and thinking_parts:
                    builder.add_step(
                        source="agent", reasoning="".join(thinking_parts),
                    )
                    thinking_parts.clear()
            elif etype == "system":
                builder.trajectory.extra.setdefault("system_events", []).append(event)
            elif etype == "result":
                builder.trajectory.extra.setdefault("results", []).append(event)
        if thinking_parts:
            builder.add_step(source="agent", reasoning="".join(thinking_parts))

    @classmethod
    def _sum_usage(
        cls, events: list[dict],
    ) -> tuple[int, int, int, int, float | None]:
        # Cumulative usage on each invocation's final result event,
        # camelCase with snake_case accepted for claude-compat drift.
        ti = to = tcr = tcc = 0
        for event in events:
            u = event.get("usage") or {}
            ti += _usage(u, "input_tokens", "inputTokens")
            to += _usage(u, "output_tokens", "outputTokens")
            tcr += _usage(u, "cache_read_input_tokens", "cacheReadTokens")
            tcc += _usage(u, "cache_creation_input_tokens", "cacheWriteTokens")
        return ti, to, tcr, tcc, None

    # ---- claude-style events ----

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
        usage = event.get("usage") or message.get("usage") or {}
        metrics = StepMetrics(
            input_tokens=_usage_opt(usage, "input_tokens", "inputTokens"),
            output_tokens=_usage_opt(usage, "output_tokens", "outputTokens"),
            cache_read_tokens=_usage_opt(
                usage, "cache_read_input_tokens", "cacheReadTokens"),
            cache_creation_tokens=_usage_opt(
                usage, "cache_creation_input_tokens", "cacheWriteTokens"),
        )
        builder.add_step(
            source="agent",
            message="\n".join(p for p in text_parts if p) or None,
            reasoning="\n".join(p for p in thinking_parts if p) or None,
            tool_calls=tool_calls,
            metrics=metrics,
        )

    @staticmethod
    def _consume_user(
        event: dict,
        builder: TrajectoryBuilder,
        drop_tool_ids: set[str] | None = None,
    ) -> None:
        message = event.get("message", {}) or {}
        results: list[ToolResult] = []
        for block in message.get("content", []) or []:
            btype = block.get("type")
            if btype == "tool_result":
                # A send's result is the peer reply — already the next
                # turn's incoming; suppress to avoid duplication.
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
                # cursor-agent echoes the stdin prompt back as a plain-text
                # user event; surfacing it would duplicate every user step.
                continue
        if not results:
            return
        builder.add_step(
            source="environment",
            observation=Observation(results=results),
        )

    # ---- composer-style tool_call events ----

    @classmethod
    def _consume_tool_call(
        cls, event: dict, builder: TrajectoryBuilder, *, drop_ids: set[str],
    ) -> bool:
        """Returns True when a send-call turn boundary closed.

        Shape (verified on 2026.07.09-c59fd9a): ``{"type":"tool_call",
        "subtype":"started"|"completed","tool_call":{"<name>ToolCall":
        {"args":{…},"result":{…}},"toolCallId":"…",…}}`` — inner dict keyed
        by tool name, call id a SIBLING of that wrapper key. Boundary closes
        at the send's ``started``; its ``completed`` result is suppressed.
        """
        subtype = event.get("subtype")
        wrapper = event.get("tool_call")
        name, body = cls._unwrap_tool_call(wrapper)
        if name is None:
            return False
        call_id = str(event.get("call_id") or "")
        if not call_id and isinstance(wrapper, dict):
            call_id = str(wrapper.get("toolCallId") or "")
        if not call_id and isinstance(body.get("args"), dict):
            call_id = str(body["args"].get("toolCallId") or "")
        is_send = cls._is_send_tool_call(name, body)

        if subtype == "started":
            builder.add_step(
                source="agent",
                tool_calls=[ToolCall(
                    id=call_id,
                    name=cls._tool_call_display_name(name, body),
                    arguments=cls._tool_call_arguments(name, body),
                )],
            )
            if is_send:
                if call_id:
                    drop_ids.add(call_id)
                return True
            return False

        if subtype == "completed":
            if is_send or (call_id and call_id in drop_ids):
                return False
            text, is_error, image_parts = cls._render_tool_result(body.get("result"))
            content: list[ContentPart] = []
            if text or not image_parts:
                content.append(ContentPart(type="text", text=text))
            content.extend(image_parts)
            builder.add_step(
                source="environment",
                observation=Observation(results=[ToolResult(
                    tool_call_id=call_id,
                    content=content,
                    is_error=is_error,
                )]),
            )
        return False

    @staticmethod
    def _unwrap_tool_call(tool_call: object) -> tuple[str | None, dict]:
        # First dict-valued entry carrying args/result wins (skips sibling
        # scalar keys like "description").
        if not isinstance(tool_call, dict):
            return None, {}
        for key, val in tool_call.items():
            if isinstance(val, dict) and ("args" in val or "result" in val):
                return key, val
        for key, val in tool_call.items():
            if isinstance(val, dict):
                return key, val
        return None, {}

    @staticmethod
    def _tool_call_display_name(wrapper_key: str, body: dict) -> str:
        # MCP wrapper key is the generic "mcpToolCall"; real name is
        # body.args.toolName (body.args.name is the server-prefixed form).
        # Built-ins encode the name in the wrapper key itself.
        args = body.get("args")
        if wrapper_key == "mcpToolCall" and isinstance(args, dict):
            for key in ("toolName", "tool_name", "name"):
                val = args.get(key)
                if isinstance(val, str) and val:
                    return val
        return wrapper_key

    @classmethod
    def _is_send_tool_call(cls, wrapper_key: str, body: dict) -> bool:
        if wrapper_key != "mcpToolCall":
            return False
        return _is_send_name(cls._tool_call_display_name(wrapper_key, body))

    @staticmethod
    def _tool_call_arguments(wrapper_key: str, body: dict) -> dict:
        # MCP calls nest the model's arguments at body.args.args; built-ins
        # keep them directly at body.args.
        args = body.get("args")
        if not isinstance(args, dict):
            return {}
        if wrapper_key == "mcpToolCall" and isinstance(args.get("args"), dict):
            return args["args"]
        return args

    @staticmethod
    def _render_tool_result(result: object) -> tuple[str, bool, list[ContentPart]]:
        # Variants observed: success / error (errorMessage) / failure (shell
        # non-zero exit). success.content is a block list ({"text":{"text":…}}
        # / {"image":{"data":…}}); split so image data never gets JSON-dumped
        # into a text part.
        image_parts: list[ContentPart] = []
        if not isinstance(result, dict):
            return ("" if result is None else json.dumps(result)), False, image_parts
        if "error" in result and isinstance(result["error"], dict):
            return (
                result["error"].get("errorMessage") or json.dumps(result["error"]),
                True,
                image_parts,
            )
        if "failure" in result and isinstance(result["failure"], dict):
            return json.dumps(result["failure"]), True, image_parts
        if "success" in result:
            succ = result["success"]
            if isinstance(succ, dict) and isinstance(succ.get("content"), list):
                text_chunks: list[str] = []
                for c in succ["content"]:
                    if not isinstance(c, dict):
                        continue
                    txt = c.get("text")
                    if isinstance(txt, dict):
                        text_chunks.append(str(txt.get("text", "")))
                    elif isinstance(txt, str):
                        text_chunks.append(txt)
                    img = c.get("image")
                    if isinstance(img, dict) and img.get("data"):
                        image_parts.append(ContentPart(
                            type="image",
                            image=ImageSource(
                                type="base64",
                                media_type=img.get("mimeType")
                                or img.get("mime_type")
                                or "image/png",
                                data=img.get("data"),
                            ),
                        ))
                return "\n".join(t for t in text_chunks if t), False, image_parts
            return (
                succ if isinstance(succ, str) else json.dumps(succ),
                False,
                image_parts,
            )
        return json.dumps(result), False, image_parts


def _usage(u: dict, *keys: str) -> int:
    for key in keys:
        val = u.get(key)
        if val is not None:
            try:
                return int(val)
            except (TypeError, ValueError):
                continue
    return 0


def _usage_opt(u: dict, *keys: str) -> int | None:
    for key in keys:
        val = u.get(key)
        if val is not None:
            try:
                return int(val)
            except (TypeError, ValueError):
                continue
    return None
