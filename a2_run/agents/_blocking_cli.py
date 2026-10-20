"""BlockingCliDeployer — shared machinery for the CLI agent harnesses.

claude_code, codex, and cursor implement the same lifecycle: one long-lived
headless CLI session per role, blocking MCP ``a2a_send_message`` as the wire,
per-turn artifacts under work_dir, host-side transcript replay. Everything
identical across them lives here;
concrete deployers keep only argv/env construction, event parsing, and
install mechanics.

Contract rules (from the refactor's call-site audit):
- DummyDeployer must NOT inherit this class: the pair lifecycle treats a
  present ``_replay_blocking_transcript`` attr as "replay expected", and
  inherited hooks count as declared.
- Replay hooks stay classmethods — the host calls them unbound off the class.
- Stdlib-only module: install_agent_deps pip-installs each concrete
  deployer package's deps, never this module's.
- Concrete class names/module paths are frozen (string-serialized into the
  sandbox spec and factory._AGENT_FQNS).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import subprocess
import time
import uuid
from pathlib import Path
from typing import ClassVar

from a2_run.base_interface import (
    AgentRunResult,
    BaseAgentDeployer,
    TrajectoryBuilder,
)

logger = logging.getLogger(__name__)


def _read_text_tolerant(path: Path) -> str:
    """Best-effort text read; never raises."""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except (FileNotFoundError, OSError):
        return ""


def _read_events(path: Path, *, harness: str = "") -> list[dict]:
    """Parse a JSONL transcript into dict events.

    Bad lines are skipped but never silently: a warning names the file and
    count (no-silent-fallbacks rule).
    """
    raw = _read_text_tolerant(path).lstrip("\ufeff")
    events: list[dict] = []
    bad = 0
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            bad += 1
            continue
        if isinstance(value, dict):
            events.append(value)
        else:
            bad += 1
    if bad:
        logger.warning(
            "%s: %d unparseable line(s) skipped in %s",
            harness or "blocking_cli", bad, path.name,
        )
    return events


class SegmentTracker:
    """Split a blocking session's act stream at each outbound send.

    Invariants: each ``a2a_send_message`` CALL closes segment i and opens
    segment i+1 with ``incomings[i+1]`` as its user step; ``incomings[0]``
    opens the stream; a send's own tool result (the peer's reply) is
    suppressed via :attr:`drop_ids` — it is already the next segment's
    incoming.
    """

    def __init__(
        self,
        builder: TrajectoryBuilder,
        incomings: list[str],
        *,
        tag_turn_index: bool = True,
    ):
        self._builder = builder
        self._incomings = incomings
        self._tag = tag_turn_index
        self.segment = 0
        self.drop_ids: set[str] = set()
        if incomings:
            self._open()

    def _open(self) -> None:
        self._builder.add_step(
            source="user",
            message=self._incomings[self.segment],
            extra={"turn_index": self.segment} if self._tag else None,
        )

    def advance(self, *, drop_id: str | None = None) -> None:
        if drop_id:
            self.drop_ids.add(drop_id)
        self.segment += 1
        if self.segment < len(self._incomings):
            self._open()


class BlockingCliDeployer(BaseAgentDeployer):
    """Shared base for CLI harnesses speaking the blocking a2a protocol."""

    default_executor: ClassVar[str] = "sandbox"
    supported_executors: ClassVar[frozenset[str]] = frozenset({"sandbox"})
    supports_a2a: ClassVar[bool] = True
    supports_session_resume: ClassVar[bool] = True

    # ---- per-harness declarations ----
    #: Log/error message prefix ("claude_code" / "codex" / "cursor").
    harness: ClassVar[str] = ""
    #: Native standing-instruction file (CLAUDE.md / AGENTS.md), rewritten
    #: to the CLI's project dir every session.
    standing_context_filename: ClassVar[str] = ""
    #: Stem of the per-turn pid file (<stem>_NNN.pid).
    pid_stem: ClassVar[str] = ""
    #: Work-dir file carrying the role's durable session/thread id.
    session_id_filename: ClassVar[str] = "session_id.txt"
    #: "host" = caller supplies the id (claude --session-id; minted before
    #: spawn, so a turn that dies pre-init stays resumable). "cli" = the CLI
    #: mints it (codex, cursor); turn 0 scrapes it from the transcript.
    session_id_mode: ClassVar[str] = "cli"
    #: Substrings in a transcript that mean the a2a transport broke
    #: mid-session (any hit fails the session — a role conversing deaf is a
    #: failure, not a degraded success). Empty = no verified pattern yet.
    transport_error_patterns: ClassVar[tuple[str, ...]] = ()
    transport_error_hint: ClassVar[str] = ""
    #: trajectory.extra key where the replay stashes authoritative usage.
    usage_events_key: ClassVar[str] = "results"
    #: True for harnesses that report tokens but never a price: record cost
    #: as unknown (None) instead of finalize()'s 0.0 ("free run") default.
    cost_unknown_when_missing: ClassVar[bool] = False
    #: Env keys whose values are whole file contents (CA bundle PEM, ADC JSON),
    #: shipped through executor.env so deployers can write them to disk in
    #: install(). They must NEVER enter a process environment: a single env
    _FILE_CONTENT_ENV_KEYS: ClassVar[frozenset[str]] = frozenset(
        {"A2_CA_BUNDLE_PEM", "GCP_ADC_JSON"}
    )

    _POLL_INTERVAL_S: ClassVar[float] = 2.0
    _TERM_GRACE_S: ClassVar[float] = 3.0

    async def launch_turn(
        self, message: str, *, turn_index: int, standing_context: str | None = None,
    ) -> AgentRunResult:
        """Run one CLI session for this role, starting or resuming a thread.

        ``standing_context`` lands as the harness's native project file —
        never a system prompt, so no harness framing exists in the agent's
        context. Subclasses supply ``_build_turn_argv`` / ``_build_env`` /
        ``_extract_turn_outputs``.
        """
        # Fail loudly before any file is written: a role that cannot launch
        # correctly must not burn its wall budget mute or identity-less.
        if turn_index < 0:
            raise RuntimeError(
                f"{self.harness}: turn_index must be non-negative, got {turn_index}"
            )
        if not standing_context:
            raise RuntimeError(
                f"{self.harness}: no standing_context — the role would launch "
                "with no identity, peer directory or completion instructions. "
                "Check pair_lifecycle._compose_standing_context for this role."
            )

        wd = Path(self.executor.work_dir)
        wd.mkdir(parents=True, exist_ok=True)
        project_dir = Path(os.path.expanduser("~"))

        # Per-turn suffixes: earlier turns' transcripts must survive (the
        # host replays every one and sums their usage).
        suffix = f"{turn_index:03d}"
        prompt_file = wd / f"prompt_{suffix}.txt"
        transcript_file = wd / f"transcript_{suffix}.jsonl"
        stderr_log = wd / f"stderr_{suffix}.log"
        pid_file = wd / f"{self.pid_stem}_{suffix}.pid"
        session_file = wd / self.session_id_filename

        # Clear residue from an earlier attempt at THIS turn only;
        # _spawn_and_wait reopens "wb", so a failed unlink can't leak.
        for f in (transcript_file, stderr_log, pid_file):
            if f.exists():
                try:
                    f.unlink()
                except OSError as e:
                    logger.warning("%s: could not remove %s: %s", self.harness, f, e)

        prompt_file.write_text(message, encoding="utf-8")
        (project_dir / self.standing_context_filename).write_text(
            standing_context, encoding="utf-8",
        )

        if self.session_id_mode == "host":
            session_id: str | None = self._resolve_session_id(
                session_file=session_file, turn_index=turn_index,
            )
            resume = turn_index > 0
        else:
            session_id = (
                self._read_resume_session_id(session_file, turn_index)
                if turn_index > 0 else None
            )
            resume = session_id is not None

        result = await self._spawn_and_wait(
            argv=self._build_turn_argv(session_id=session_id, resume=resume),
            prompt_file=prompt_file,
            transcript_file=transcript_file,
            stderr_log=stderr_log,
            pid_file=pid_file,
            env=self._build_env(),
            cwd=project_dir,
        )

        self._check_transport_health(result, transcript_file)

        reply_text, observed_id = self._extract_turn_outputs(transcript_file)
        result.reply_text = reply_text
        if self.session_id_mode == "host":
            # The id we passed is authoritative; a mismatch means the next
            # resume would land in the wrong conversation.
            result.session_id = session_id
            if observed_id and observed_id != session_id:
                logger.warning(
                    "%s: turn %d ran under session %s but %s was requested",
                    self.harness, turn_index, observed_id, session_id,
                )
        else:
            sid = observed_id or session_id
            result.session_id = sid
            if turn_index == 0 and sid:
                session_file.write_text(sid, encoding="utf-8")
            elif turn_index == 0 and result.status == "completed":
                logger.warning(
                    "%s: turn 0 completed without a session id; follow-ups "
                    "cannot resume this role", self.harness,
                )
        if result.status == "completed" and reply_text is None:
            logger.warning(
                "%s: turn %d completed without a result text",
                self.harness, turn_index,
            )
        return result

    def _resolve_session_id(self, *, session_file: Path, turn_index: int) -> str:
        # A missing id file on a later turn means the work_dir was not
        # preserved — raise; minting a fresh id would silently drop the
        # thread's history.
        if turn_index == 0:
            session_id = str(uuid.uuid4())
            session_file.write_text(session_id, encoding="utf-8")
            return session_id
        try:
            session_id = session_file.read_text(encoding="utf-8").strip()
        except OSError as e:
            raise RuntimeError(
                f"{self.harness}: cannot resume turn {turn_index} — no session "
                f"id at {session_file} ({e}). Turn 0 writes it, so the role's "
                "work_dir was not preserved across turns."
            ) from e
        if not session_id:
            raise RuntimeError(
                f"{self.harness}: cannot resume turn {turn_index} — session id "
                f"file {session_file} is empty"
            )
        return session_id

    def _read_resume_session_id(self, session_file: Path, turn_index: int) -> str:
        try:
            session_id = session_file.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise RuntimeError(
                f"{self.harness}: cannot resume turn {turn_index} — no session "
                f"id at {session_file}. Turn 0 writes it, so either that "
                "session died before its init event or the role's work_dir "
                "was not preserved across turns."
            ) from exc
        if not session_id:
            raise RuntimeError(
                f"{self.harness}: cannot resume turn {turn_index} — session id "
                f"file {session_file} is empty"
            )
        return session_id

    # ---- per-harness hooks ----

    def _build_turn_argv(
        self, *, session_id: str | None, resume: bool,
    ) -> list[str]:
        raise NotImplementedError

    def _build_env(self) -> dict[str, str]:
        # Executor-env overlay is belt-and-braces for the local executor;
        # _sandbox_entry already merged it into os.environ when sandboxed.
        env = os.environ.copy()
        for k, v in (self.executor.env or {}).items():
            env[k] = v
        # Whole-file blobs (CA bundle, ADC) reach a deployer via executor.env
        # but must never ride in a process env — one >128KB string trips E2BIG
        # on the next execve and takes down the whole harness.
        for k in self._FILE_CONTENT_ENV_KEYS:
            env.pop(k, None)
        env["NO_COLOR"] = "1"
        return env

    @staticmethod
    def _extract_turn_outputs(transcript: Path) -> tuple[str | None, str | None]:
        """(reply_text, session_id) — diagnostics only; the wire is the hub."""
        raise NotImplementedError

    def _classify_failure(self, stderr_text: str, tx_text: str) -> list[str]:
        return []

    def _check_transport_health(
        self, result: AgentRunResult, transcript_file: Path,
    ) -> None:
        # A broken transport (e.g. client-side MCP timeout eating the peer's
        # reply) recorded as "completed" masquerades harness breakage as
        # model behaviour — fail the session instead.
        if not self.transport_error_patterns:
            return
        text = _read_text_tolerant(transcript_file)
        hits = {p: text.count(p) for p in self.transport_error_patterns if p in text}
        if not hits:
            return
        result.status = "failed"
        detail = ", ".join(f"{n}x {p!r}" for p, n in hits.items())
        transport_err = (
            f"{self.harness}: transport-level error(s) in transcript ({detail}) "
            "— the a2a session broke mid-conversation and is not trustworthy "
            "past the first occurrence."
        )
        if self.transport_error_hint:
            transport_err += f" {self.transport_error_hint}"
        result.error = (
            f"{transport_err} | {result.error}" if result.error else transport_err
        )

    async def _spawn_and_wait(
        self,
        *,
        argv: list[str],
        prompt_file: Path,
        transcript_file: Path,
        stderr_log: Path,
        pid_file: Path,
        env: dict[str, str],
        cwd: Path,
    ) -> AgentRunResult:
        """Spawn the CLI (stdin=prompt, stdout=transcript) and wait it out."""
        t0 = time.monotonic()
        with open(prompt_file, "rb") as pin, \
             open(transcript_file, "wb") as tout, \
             open(stderr_log, "wb") as terr:
            proc = await asyncio.to_thread(
                subprocess.Popen,
                argv,
                stdin=pin,
                stdout=tout,
                stderr=terr,
                env=env,
                cwd=str(cwd),
                start_new_session=hasattr(os, "setsid"),
            )
        pid_file.write_text(str(proc.pid), encoding="ascii")
        logger.info("%s: spawned pid=%s argv0=%s", self.harness, proc.pid, argv[0])

        # Wall budget is orchestration-owned (executor wraps launch_turn in
        # asyncio.wait_for); on cancel, reap the whole process group so the
        # MCP server can't outlive the run holding its hub connection.
        try:
            while proc.poll() is None:
                await asyncio.sleep(self._POLL_INTERVAL_S)
        except asyncio.CancelledError:
            self._terminate_proc_group(proc, force=False)
            try:
                await asyncio.wait_for(
                    asyncio.to_thread(proc.wait), timeout=self._TERM_GRACE_S,
                )
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._terminate_proc_group(proc, force=True)
            raise

        duration_s = time.monotonic() - t0
        exit_code = proc.returncode
        status = "completed" if exit_code == 0 else "failed"
        error: str | None = None
        if status == "failed":
            error = self._diagnose_failure(
                stderr_log=stderr_log,
                transcript=transcript_file,
                exit_code=exit_code,
            )
        return AgentRunResult(
            status=status,
            pid=proc.pid,
            exit_code=exit_code,
            transcript_path=str(transcript_file),
            stderr_path=str(stderr_log),
            duration_s=duration_s,
            error=error,
        )

    @staticmethod
    def _terminate_proc_group(proc: subprocess.Popen, *, force: bool) -> None:
        # Signal the GROUP: proc.terminate() reaps only the direct child,
        # orphaning the MCP server and any tool subprocesses.
        try:
            if hasattr(os, "killpg") and hasattr(os, "getpgid"):
                os.killpg(
                    os.getpgid(proc.pid),
                    signal.SIGKILL if force else signal.SIGTERM,
                )
            elif force:
                proc.kill()
            else:
                proc.terminate()
        except (OSError, ProcessLookupError):
            pass

    def _diagnose_failure(
        self, *, stderr_log: Path, transcript: Path, exit_code: int | None,
    ) -> str:
        parts = [f"{self.harness}: agent failed (rc={exit_code})"]
        stderr_text = _read_text_tolerant(stderr_log)
        tx_text = _read_text_tolerant(transcript)
        parts.append(f"stderr={len(stderr_text)}B transcript={len(tx_text)}B")
        if stderr_text.strip():
            parts.append(f"stderr tail: ...{stderr_text[-800:]}")
        parts.extend(self._classify_failure(stderr_text, tx_text))
        if tx_text.strip():
            parts.append(f"transcript tail: ...{tx_text[-800:]}")
        return " | ".join(parts)

    def _a2a_server_and_env(self) -> tuple[Path, dict[str, str]]:
        """The a2a stdio MCP server script and its env, validated.

        These tools ARE the wire, so a missing server script or a missing
        A2A_SELF identity is fatal here rather than surfacing minutes later as
        an apparent model failure. A2A_PEERS (the recipient roster) is required
        too UNLESS this role has no reachable peers — a single-role solo run or
        a role whose visibility edges are all removed — which the pair
        lifecycle marks with A2A_NO_PEERS. Without that marker an absent roster
        still fails loudly (a real plumbing bug), so the solo relaxation cannot
        mask one.
        """
        sandbox = self.executor.sandbox
        server = Path(sandbox.mcp_server_dir) / "a2a_tools_mcp.py"
        if not server.is_file():
            raise RuntimeError(
                f"{self.harness}: no a2a tools MCP server at {server} — "
                "a2a_send_message/a2a_task_complete would be unavailable and "
                "the role could not reach its peer. Check the sandbox image "
                "baked a2a_tools_mcp.py into mcp_server_dir "
                "(docker/ubuntu-base/)."
            )
        tool_env = {"A2A_HUB_URL": f"http://127.0.0.1:{sandbox.hub_port}"}
        for key in ("A2A_SELF", "A2A_PEERS", "A2A_PEER_CARDS", "A2A_CAN_COMPLETE"):
            val = (self.executor.env or {}).get(key) or os.environ.get(key)
            if val:
                tool_env[key] = val
        # A peerless role (solo run / fully-isolated by visibility) is marked
        # explicitly by the pair lifecycle; there A2A_PEERS is legitimately
        # absent and only A2A_SELF is required.
        no_peers = ((self.executor.env or {}).get("A2A_NO_PEERS")
                    or os.environ.get("A2A_NO_PEERS")) == "1"
        required = ("A2A_SELF",) if no_peers else ("A2A_SELF", "A2A_PEERS")
        missing = [k for k in required if k not in tool_env]
        if missing:
            raise RuntimeError(
                f"{self.harness}: missing required a2a env {missing} — the MCP "
                "tool cannot validate message recipients. Check the pair "
                "lifecycle populated executor.env for this role."
            )
        return server, tool_env

    def _a2a_mcp_servers_dict(self) -> dict:
        # Server name "a2a": tools surface with the server's own a2a_ prefix,
        # avoiding collision with a harness's built-in messaging tools.
        server, tool_env = self._a2a_server_and_env()
        return {
            "mcpServers": {
                "a2a": {
                    "command": self.executor.sandbox.python,
                    "args": [str(server)],
                    "env": tool_env,
                },
            },
        }

    # ---- host-side replay scaffolding (classmethods — called unbound) ----

    @classmethod
    def _session_transcripts(cls, transcript_file: Path) -> list[Path]:
        """All of this role's session transcripts, numerically sorted."""
        def turn_of(p: Path) -> int | None:
            tail = p.stem.rsplit("_", 1)[-1]
            return int(tail) if tail.isdigit() else None

        found = [
            (n, p) for p in transcript_file.parent.glob("transcript_*.jsonl")
            if (n := turn_of(p)) is not None
        ]
        if not found:
            return [transcript_file]
        return [p for _n, p in sorted(found)]

    @classmethod
    def _load_session_events(
        cls, transcript_file: Path, builder: TrajectoryBuilder,
    ) -> list[dict]:
        """Concatenate all session transcripts into one event stream.

        Session boundaries are NOT turn boundaries (incomings is indexed by
        messages sent); a resume is recorded in extra['sessions'] only.
        """
        events: list[dict] = []
        sessions_meta: list[dict] = []
        for path in cls._session_transcripts(transcript_file):
            session_events = _read_events(path, harness=cls.harness)
            events.extend(session_events)
            sessions_meta.append({
                "transcript": path.name,
                "events": len(session_events),
            })
        if len(sessions_meta) > 1:
            builder.trajectory.extra["sessions"] = sessions_meta
        return events

    @classmethod
    def apply_authoritative_metrics(
        cls, builder: TrajectoryBuilder, *, expected_results: int | None = None,
    ) -> None:
        """Install summed authoritative usage; call once, AFTER the replay.

        The per-step sum finalize() would use is lossy for every harness.
        Fewer usage events than sessions ⇒ totals are a lower bound, flagged
        partial. ``expected_results`` is the caller's belief (host passes 1);
        the replay's own extra['sessions'] count wins.
        """
        n_sessions = len(builder.trajectory.extra.get("sessions") or [])
        if n_sessions > (expected_results or 0):
            expected_results = n_sessions
        events = builder.trajectory.extra.get(cls.usage_events_key) or []
        if not events:
            if expected_results:
                builder.mark_metrics_partial()
            return
        ti, to, tcr, tcc, cost = cls._sum_usage(events)
        builder.override_final_metrics(
            total_input_tokens=ti,
            total_output_tokens=to,
            total_cache_read_tokens=tcr,
            total_cache_creation_tokens=tcc,
            total_cost_usd=cost,
        )
        if cost is None and cls.cost_unknown_when_missing:
            builder.set_cost_unknown()
        if expected_results is not None and len(events) < expected_results:
            builder.mark_metrics_partial()

    @classmethod
    def _sum_usage(
        cls, events: list[dict],
    ) -> tuple[int, int, int, int, float | None]:
        """→ (input, output, cache_read, cache_creation, cost_or_None)."""
        raise NotImplementedError
