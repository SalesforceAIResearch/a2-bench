"""Read a finished run directory into one tolerant :class:`RunView`.

A pair (a2a) run dir written by :class:`a2_run.orchestration.run_writer.RunWriter`
holds the JSON files the viewer cares about::

    run.json            run metadata (roles, task, status, score, timings)
    conversation.json   first_speaker, channel, termination, ordered turns
    trajectory_<role>.json   one ATIF trajectory per role (the fine-grained steps)
    eval_result.json    the task's score + eval status (+ its rubric breakdown)
    metrics.json        offline, task-agnostic metrics (optional; see
                        :mod:`metrics`) — absent until a run is scored by them

plus the orchestrator's own lifecycle log::

    events.jsonl        run/provision/session records written by the runner

This module reads them back with **zero** dependency on the pydantic schema and
never raises on a missing/short/garbled file — a viewer's whole job is to show
what is there, so every accessor degrades to a sensible empty default. The
result is a plain dataclass the renderer walks; discovery over a whole ``.logs``
tree is :func:`iter_runs`.

**Fidelity contract.** Everything an agent emitted or received reaches the
renderer intact: no text is truncated, no step is dropped, no field is
summarised away. That covers reasoning, tool arguments, tool results, messages,
images, per-step usage, the deployer's per-step ``extra``, trajectory-level
``extra`` (CLI init event, authoritative ``result`` events, session boundaries),
sub-agent trajectories, and activity recorded after the last relayed message.
Presentation may collapse long content behind a disclosure; it may not discard
it.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

logger = logging.getLogger(__name__)


def _read_json(path: Path) -> dict[str, Any]:
    """Load one JSON object; return ``{}`` on any miss (absent / unreadable / not-a-dict)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        if path.exists():
            logger.warning("transcript: could not parse %s: %s", path, e)
        return {}
    return data if isinstance(data, dict) else {}


@dataclass
class Turn:
    """One relay hop from ``conversation.json`` (a single agent turn)."""

    round: int
    role: str
    turn_index: int
    incoming: str
    reply: str
    status: str
    ended: bool
    duration_s: float | None
    error: str | None
    raw: dict[str, Any] = field(default_factory=dict)
    """The turn's entry from ``conversation.json``, verbatim — so a field the
    relay grows (or a task-specific one) is still reachable by the viewer
    without a change here."""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Turn":
        return cls(
            round=int(d.get("round", 0)),
            role=str(d.get("role", "?")),
            turn_index=int(d.get("turn_index", 0)),
            incoming=str(d.get("incoming") or ""),
            reply=str(d.get("reply") or ""),
            status=str(d.get("status") or "unknown"),
            ended=bool(d.get("ended")),
            duration_s=d.get("duration_s"),
            error=d.get("error"),
            raw=dict(d) if isinstance(d, dict) else {},
        )


@dataclass
class ImageRef:
    """One image an agent emitted or received, as referenced by the trajectory.

    ``kind`` mirrors :class:`trajectory.ImageSource`: ``path`` (relative to the
    run dir — the persisted form), ``url``, or ``data`` (inline base64, which
    ``persist_screenshots`` normally rewrites to ``path`` before the trajectory
    lands on disk, but is rendered inline when it survives).
    """

    kind: str
    src: str
    media_type: str = "image/png"
    alt: str = ""


@dataclass
class Act:
    """One thing an agent did *within* a turn on the way to its reply.

    ``kind`` is one of ``reason`` (thinking), ``tool`` (a tool call, with
    ``tool_name`` / ``arguments``), ``obs`` / ``obs_err`` (a tool result),
    ``note`` (an assistant message), ``env`` (an environment text update),
    ``sys`` (a framework/system note), or ``event`` (a step that recorded only
    structured metadata, e.g. a codex ``file_change``).

    Every text field here is the **full** recorded text: nothing on this path
    truncates, summarises or reorders what the agent emitted.
    """

    kind: str
    text: str = ""
    tool_name: str = ""
    tool_args: str = ""
    """The tool call's arguments as one JSON blob. Kept for callers that want a
    single string; :attr:`arguments` is the structured form the viewer renders
    (which shows long string values raw rather than JSON-escaped)."""
    arguments: dict[str, Any] = field(default_factory=dict)
    tool_call_id: str = ""
    images: list[ImageRef] = field(default_factory=list)
    empty: bool = False
    """True when the agent/env produced this act with no content at all — an
    empty tool result, say. Recorded rather than dropped: "the tool returned
    nothing" is itself an observation."""


@dataclass
class Metrics:
    """Token + cost accounting for one turn, one role, or a whole run.

    Tokens are always exact integers (0 when unknown). ``cost_usd`` is ``None``
    when no cost was reported at all and a float otherwise. ``partial`` marks a
    total that is known to be a lower bound — a turn was killed before it
    emitted authoritative usage (see :attr:`FinalMetrics.metrics_partial`).
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    cost_usd: float | None = None
    partial: bool = False

    @property
    def total_tokens(self) -> int:
        return (self.input_tokens + self.output_tokens
                + self.cache_read_tokens + self.cache_creation_tokens)

    @property
    def has_data(self) -> bool:
        """True if there's anything worth showing (some tokens or a cost)."""
        return self.total_tokens > 0 or bool(self.cost_usd)

    def __add__(self, other: "Metrics") -> "Metrics":
        cost: float | None = None
        if self.cost_usd is not None or other.cost_usd is not None:
            cost = (self.cost_usd or 0.0) + (other.cost_usd or 0.0)
        return Metrics(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cache_creation_tokens=self.cache_creation_tokens + other.cache_creation_tokens,
            cost_usd=cost,
            partial=self.partial or other.partial,
        )


def _int(x: Any) -> int:
    try:
        return int(x or 0)
    except (TypeError, ValueError):
        return 0


def _metrics_from_final(fm: dict[str, Any]) -> Metrics:
    """The authoritative per-role totals written to ``trajectory.final_metrics``."""
    return Metrics(
        input_tokens=_int(fm.get("total_input_tokens")),
        output_tokens=_int(fm.get("total_output_tokens")),
        cache_read_tokens=_int(fm.get("total_cache_read_tokens")),
        cache_creation_tokens=_int(fm.get("total_cache_creation_tokens")),
        cost_usd=fm.get("total_cost_usd"),
        partial=bool(fm.get("metrics_partial")),
    )


def _metrics_from_result(event: dict[str, Any]) -> Metrics:
    """One turn's authoritative usage, from a claude-code ``result`` event.

    Note the wire keys differ from ``final_metrics``: usage uses
    ``cache_read_input_tokens`` / ``cache_creation_input_tokens``.
    """
    u = event.get("usage") or {}
    return Metrics(
        input_tokens=_int(u.get("input_tokens")),
        output_tokens=_int(u.get("output_tokens")),
        cache_read_tokens=_int(u.get("cache_read_input_tokens")),
        cache_creation_tokens=_int(u.get("cache_creation_input_tokens")),
        cost_usd=event.get("total_cost_usd"),
    )


@dataclass
class StepView:
    """One trajectory step with its envelope intact, plus the acts it produced.

    The envelope — ``source``, ``step_id``, ``timestamp``, per-step usage and
    the deployer's ``extra`` (``stop_reason``, ``item_id``, ``exit_code``,
    ``server``, ``turn_index``, …) — is part of what the run recorded, so it
    travels with the acts rather than being flattened away.
    """

    step_id: int
    source: str
    timestamp: str = ""
    acts: list[Act] = field(default_factory=list)
    metrics: Metrics | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class TurnBlock:
    """One turn in the merged timeline: what the agent *did*, then what it *said*.

    Built by joining each ``conversation.json`` turn to the matching segment of
    that role's trajectory (the steps between successive received messages).
    Nothing recorded in that segment is dropped: every step it holds is carried
    in :attr:`steps` with its envelope, and :attr:`acts` is the flat view of the
    same content for callers that only want the sequence.
    """

    role: str
    round: int
    duration_s: float | None
    ended: bool
    status: str
    error: str | None
    steps: list[StepView]
    reply: str
    incoming: str = ""
    """The message this turn's agent received, exactly as it read it (``from:``
    header included). It is the peer's delivered text, so it is also the
    previous block's ``reply`` — shown as the turn's inbound so each turn can be
    read on its own, and so a channel rewrite between emit and delivery is
    visible rather than implied."""
    metrics: Metrics | None = None
    """This turn's token/cost usage, from the role's per-turn ``result`` event
    (matched by ``turn_index``). ``None`` when the agent reported no usage for
    the turn (e.g. the dummy responder, or a turn killed before its report)."""
    reply_is_emitted: bool = False
    """True when :attr:`reply` is byte-identical to the last message the agent
    emitted in this segment. When False the delivered text and the emitted text
    diverged (a channel transform, or the reply came from elsewhere) — which the
    viewer surfaces rather than silently preferring one."""
    sent_to: str = ""
    """Recipient of this turn's outbound message, from the ``a2a_send_message``
    call's ``to`` argument. Lets the transcript name the handoff ("→ registry")
    instead of leaving the reader to infer who the next speaker is."""
    emitted: str = ""
    """The outbound body the agent actually passed to ``a2a_send_message``.
    Equal to :attr:`reply` in the ordinary case; kept separately so a channel
    rewrite can be shown as a diff instead of the viewer having to pick one
    of the two."""
    trailing: bool = False
    """True for a block holding trajectory activity that followed the role's
    last relayed message (e.g. filing an answer, then ``a2a_task_complete``),
    or any segment with no matching ``conversation.json`` turn. Such steps are
    real recorded work, so they get their own block instead of being
    discarded."""
    raw_turn: dict[str, Any] = field(default_factory=dict)
    """The originating ``conversation.json`` entry, verbatim ({} for a trailing
    block)."""

    @property
    def acts(self) -> list[Act]:
        """Every act in the turn, flattened in recorded order."""
        return [a for s in self.steps for a in s.acts]


@dataclass
class RunView:
    """Everything the viewer needs about one run, read from its directory.

    The four ``*.json`` files are kept as raw dicts; typed accessors below pull
    out the handful of fields the renderer uses, each with a safe default so a
    partial run (e.g. crashed before ``conversation.json`` was written) still
    renders.
    """

    run_dir: Path
    run: dict[str, Any] = field(default_factory=dict)
    conversation: dict[str, Any] = field(default_factory=dict)
    eval: dict[str, Any] = field(default_factory=dict)
    trajectories: dict[str, dict[str, Any]] = field(default_factory=dict)
    metrics_report: dict[str, Any] = field(default_factory=dict)
    """``metrics.json`` — the offline, task-agnostic metrics computed for this
    run by the :mod:`metrics` package (interaction conduct, per role). Empty for
    a run that has not been scored by them; see :meth:`metric_results`."""

    # ---- identity -----------------------------------------------------------
    @property
    def run_id(self) -> str:
        return self.run.get("run_id") or self.run_dir.name

    @property
    def timestamp(self) -> str:
        """The run's ``YYYYMMDD_HHMMSS`` stamp — its leaf dir name.

        This is when the run started (:class:`RunWriter` names the leaf dir with
        it), and because the format is fixed-width it also sorts chronologically
        as a plain string. ``run.json``'s ``timestamp_utc`` is the fallback for a
        dir that wasn't named by the writer.
        """
        return self.run_dir.name or self.run.get("timestamp_utc", "")

    @property
    def task_path(self) -> str:
        return (self.run.get("task") or {}).get("path") or (self.run.get("task") or {}).get("slug", "")

    @property
    def task_slug(self) -> str:
        return (self.run.get("task") or {}).get("slug", "") or self.task_path

    @property
    def variant_index(self) -> int:
        return int((self.run.get("task") or {}).get("variant_index", 0))

    @property
    def variant_name(self) -> str:
        """Declared variant name if recorded."""
        name = str((self.run.get("task") or {}).get("variant_name") or "").strip()
        if name:
            return name
        seg = self.run_dir.parent.name if self.run_dir.parent else ""
        return seg or f"v{self.variant_index}"

    # ---- outcome ------------------------------------------------------------
    @property
    def status(self) -> str:
        return self.run.get("status") or "unknown"

    @property
    def score(self) -> float | None:
        # eval_result.json is the authoritative score; run.json mirrors it.
        s = self.eval.get("score")
        return s if s is not None else self.run.get("score")

    @property
    def duration_s(self) -> float | None:
        return (self.run.get("timings") or {}).get("duration_s")

    @property
    def termination_reason(self) -> str | None:
        return (self.conversation.get("termination") or {}).get("reason") \
            or (self.run.get("conversation") or {}).get("termination_reason")

    @property
    def end_reason(self) -> str | None:
        return (self.conversation.get("termination") or {}).get("end_reason") \
            or (self.run.get("conversation") or {}).get("end_reason")

    @property
    def channel(self) -> str:
        return self.conversation.get("channel", "")

    @property
    def first_speaker(self) -> str:
        return self.conversation.get("first_speaker", "")

    # ---- participants -------------------------------------------------------
    @property
    def roles(self) -> dict[str, dict[str, Any]]:
        """role -> {id, class, name, model, executor, ...} from run.json."""
        return self.run.get("roles") or {}

    def role_order(self) -> list[str]:
        """Stable role order: first_speaker leads, then the rest as declared."""
        roles = list(self.roles) or self._roles_from_turns()
        fs = self.first_speaker
        if fs in roles:
            roles = [fs] + [r for r in roles if r != fs]
        return roles

    def _roles_from_turns(self) -> list[str]:
        seen: list[str] = []
        for t in self.turns:
            if t.role not in seen:
                seen.append(t.role)
        return seen

    # ---- content ------------------------------------------------------------
    @property
    def turns(self) -> list[Turn]:
        return [Turn.from_dict(t) for t in self.conversation.get("turns", [])]

    def trajectory(self, role: str) -> dict[str, Any]:
        return self.trajectories.get(role, {})

    # ---- metrics ------------------------------------------------------------
    def role_metrics(self, role: str) -> Metrics:
        """A role's authoritative token/cost totals (from its ``final_metrics``)."""
        return _metrics_from_final(self.trajectory(role).get("final_metrics") or {})

    def total_metrics(self) -> Metrics:
        """Run-wide totals: the sum of every role's authoritative totals."""
        total = Metrics()
        for role in self.role_order():
            total = total + self.role_metrics(role)
        return total

    def _turn_metrics(self, role: str) -> list[Metrics]:
        """A role's per-turn usage, indexed by that role's ``turn_index``.

        Sourced from ``trajectory.extra.results`` — the CLI ``result`` events.
        Per-turn attribution is only meaningful when there is ONE result per
        turn (the old turn-per-invocation model). The blocking-``a2a_send_message``
        model runs ONE invocation for the whole session, so its single result
        covers ALL turns — attributing it to any one turn would misreport the
        rest as free. In that case return an empty list: the per-turn cards
        show no cost and the role/run totals (from ``final_metrics``) carry the
        authoritative figure instead.
        """
        results = [r for r in
                   ((self.trajectory(role).get("extra") or {}).get("results") or [])
                   if isinstance(r, dict)]
        n_turns = sum(1 for t in self.turns if t.role == role)
        if len(results) != n_turns:
            return []
        return [_metrics_from_result(r) for r in results]

    # ---- offline metrics ----------------------------------------------------
    def metric_results(self) -> dict[str, dict[str, Any]]:
        """``metric name -> its result`` from ``metrics.json`` (``{}`` if absent).

        Each result carries ``scores`` (run-level rollups), ``roles``
        (per-role scores), ``status``, ``summary`` and ``explanation`` — see
        :class:`metrics.base.MetricResult`. Read generically: the viewer
        discovers which metrics and score keys exist from the file rather than
        naming any, so a metric added to that package needs no viewer change.
        """
        results = self.metrics_report.get("results")
        if not isinstance(results, dict):
            return {}
        return {k: v for k, v in results.items() if isinstance(v, dict)}

    def metric_score(self, metric: str, key: str) -> float | None:
        """One run-level metric score, or ``None`` when absent/non-numeric."""
        r = self.metric_results().get(metric) or {}
        v = (r.get("scores") or {}).get(key)
        if isinstance(v, bool):          # bool is an int subclass — keep it out
            return float(v)
        return float(v) if isinstance(v, (int, float)) else None

    # ---- raw records --------------------------------------------------------
    def role_extra(self, role: str) -> dict[str, Any]:
        """A role's trajectory-level ``extra``, verbatim.

        This is where deployers park what doesn't fit a Step: the CLI's
        ``system`` init event (model, tools, MCP servers, permission mode), the
        authoritative ``result`` events, session/resume boundaries, codex thread
        ids, and any ``replay_degraded`` / ``metrics_error`` flags. All of it is
        run evidence, so the viewer shows it instead of the loader deciding
        which keys matter.
        """
        extra = self.trajectory(role).get("extra")
        return extra if isinstance(extra, dict) else {}

    def role_agent_info(self, role: str) -> dict[str, Any]:
        """A role's ``agent`` block (name / version / model / extra)."""
        info = self.trajectory(role).get("agent")
        return info if isinstance(info, dict) else {}

    def role_final(self, role: str) -> dict[str, Any]:
        """A role's ``final_metrics`` block, verbatim (status, reward, totals)."""
        fm = self.trajectory(role).get("final_metrics")
        return fm if isinstance(fm, dict) else {}

    def role_timings(self, role: str) -> tuple[str, str]:
        """A role's trajectory ``(started_at, ended_at)``."""
        traj = self.trajectory(role)
        return str(traj.get("started_at") or ""), str(traj.get("ended_at") or "")

    def subagents(self, role: str) -> list[dict[str, Any]]:
        """A role's sub-agent trajectories (e.g. claude-code ``Task`` tool).

        Recorded by the schema and previously never rendered — a whole nested
        episode that the transcript simply did not mention.
        """
        subs = self.trajectory(role).get("subagent_trajectories")
        return [s for s in subs if isinstance(s, dict)] if isinstance(subs, list) else []

    def events(self) -> list[dict[str, Any]]:
        """The orchestrator's ``events.jsonl`` — the run's own lifecycle log.

        Provisioning, session start/finish per role, and the terminal
        run_completed/failed record with its status and score. Written by the
        runner rather than by any agent, so it is the only place some failures
        (a role that never started) are visible at all.
        """
        path = self.run_dir / "events.jsonl"
        out: list[dict[str, Any]] = []
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return out
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                # A torn last line (run still writing) — keep it visible as text
                # rather than dropping the record.
                out.append({"type": "unparsed", "raw": line})
                continue
            if isinstance(obj, dict):
                out.append(obj)
        return out

    def artifacts(self) -> list[tuple[str, int]]:
        """``(relative path, size)`` for every file in the run dir, sorted.

        Lets the page point at the raw record behind the render — the agent's
        own ``transcript_000.jsonl``, its prompt, stderr, task output files —
        so a reader can always get to the unprocessed source.
        """
        out: list[tuple[str, int]] = []
        for p in sorted(self.run_dir.rglob("*")):
            if not p.is_file():
                continue
            try:
                out.append((str(p.relative_to(self.run_dir)), p.stat().st_size))
            except (OSError, ValueError):
                continue
        return out

    def briefs(self) -> dict[str, str]:
        """role -> the standing brief/system prompt it was given (from its trajectory)."""
        out: dict[str, str] = {}
        for role in self.role_order():
            brief = (self.trajectory(role).get("instruction") or "").strip()
            if brief:
                out[role] = brief
        return out

    def timeline(self) -> list["TurnBlock"]:
        """The merged timeline: one :class:`TurnBlock` per turn, nothing dropped.

        Each turn's steps come from the slice of the role's trajectory between
        the message it received and the next one. Two fidelity rules matter
        here:

        * The emitted message is **not** removed just because it matches the
          delivered ``reply``. It is flagged instead
          (:attr:`TurnBlock.reply_is_emitted`) so the viewer can render it once
          while still being able to show a divergence when the channel rewrote
          the text.
        * Trajectory segments with no matching turn — the work a role did after
          its last relayed message (filing an answer, ``a2a_task_complete``),
          or steps recorded for a role the conversation never logged — become
          trailing blocks rather than being discarded.
        """
        # Per-role queue of step-segments, one per turn, in turn order.
        segments: dict[str, list[list[StepView]]] = {
            role: _segment_steps(self.trajectory(role)) for role in self.role_order()
        }
        # Per-role per-turn usage, indexed by that role's turn_index.
        turn_metrics: dict[str, list[Metrics]] = {
            role: self._turn_metrics(role) for role in self.role_order()
        }
        cursor: dict[str, int] = {role: 0 for role in segments}

        blocks: list[TurnBlock] = []
        for t in self.turns:
            role_segs = segments.get(t.role, [])
            i = cursor.get(t.role, 0)
            steps = list(role_segs[i]) if i < len(role_segs) else []
            cursor[t.role] = i + 1
            role_tm = turn_metrics.get(t.role, [])
            m = role_tm[t.turn_index] if 0 <= t.turn_index < len(role_tm) else None
            out = _outbound(steps)
            blocks.append(TurnBlock(
                role=t.role, round=t.round, duration_s=t.duration_s,
                ended=t.ended, status=t.status, error=t.error,
                steps=steps, reply=t.reply, incoming=t.incoming,
                metrics=m if (m and m.has_data) else None,
                reply_is_emitted=bool(out) and out[0].strip() == (t.reply or "").strip()
                                 and bool((t.reply or "").strip()),
                sent_to=out[1] if out else "",
                emitted=out[0] if out else "",
                raw_turn=t.raw,
            ))

        # Anything the trajectories recorded past the last relayed turn.
        for role, role_segs in segments.items():
            for seg in role_segs[cursor.get(role, 0):]:
                if not seg:
                    continue
                blocks.append(TurnBlock(
                    role=role, round=blocks[-1].round if blocks else 0,
                    duration_s=None, ended=False, status="completed", error=None,
                    steps=seg, reply="", incoming="", trailing=True,
                ))
        return blocks


#: Suffix identifying the a2a relay's outbound tool, however the MCP server
#: namespaced it (``a2a_send_message``, ``mcp__a2a__a2a_send_message``, …).
#: The ``a2a_`` prefix on the bare name is deliberate: several harnesses ship
#: their own native ``send_message`` for agent-team collaboration, and a
#: suffix match against the bare name would misidentify a call to THAT tool
#: as this relay's. Matches the deployers' own convention (see
#: ``_SEND_TOOL_SUFFIX`` in the claude-code deployer and ``_is_send_message``
#: in codex).
_SEND_SUFFIX = "a2a_send_message"

#: The relay tool argument carrying the message body.
_CONTENT_ARG = "content"


def _outbound(steps: list["StepView"]) -> tuple[str, str] | None:
    """The ``(body, recipient)`` this segment last handed to ``a2a_send_message``.

    The relay takes its delivered text from the ``a2a_send_message`` call's
    ``content`` argument, so that argument — not the assistant's closing prose —
    is what "emitted" means for an a2a turn. Falls back to the last assistant
    text (with no recipient) for harnesses that reply without the relay tool,
    e.g. the dummy responder. ``None`` when the segment emitted nothing.
    """
    for step in reversed(steps):
        for act in reversed(step.acts):
            if act.kind == "tool" and act.tool_name.endswith(_SEND_SUFFIX):
                content = act.arguments.get(_CONTENT_ARG)
                if isinstance(content, str):
                    to = act.arguments.get("to")
                    return content, (to if isinstance(to, str) else "")
    for step in reversed(steps):
        for act in reversed(step.acts):
            if act.kind == "note" and act.text.strip():
                return act.text, ""
    return None



# The a2a relay tools — ``a2a_send_message`` (carries a turn's message to the
# peer) and ``a2a_task_complete`` (ends the run) — ARE the protocol surface the
# agent actually drives, so the transcript renders them verbatim rather than
# hiding them: the tool call with its full arguments (``to`` / ``content`` /
# ``outcome`` and any future protocol variables) and, for ``a2a_task_complete``,
# its ack observation. The reply block shows what the peer was DELIVERED (post
# channel transform); the a2a_send_message call shows what the agent
# EMITTED — the two diverge the moment a channel rewrites content or the
# message schema grows
# a field, and the transcript must surface that.
#
# Nothing on this path is clipped. The viewer's contract is that every byte an
# agent emitted or received is present in the page: reasoning, tool arguments,
# tool results and messages are carried at full length and collapsed in the UI
# when long, so "long" costs a click rather than the content itself. Anything
# dropped here would be invisible — there is no second place to go look.


def _images_from_content(content: Any) -> list[ImageRef]:
    """Every image referenced by a list of ATIF ContentParts."""
    out: list[ImageRef] = []
    if not isinstance(content, list):
        return out
    for cp in content:
        if not isinstance(cp, dict) or cp.get("type") != "image":
            continue
        img = cp.get("image")
        if not isinstance(img, dict):
            continue
        kind = str(img.get("type") or "path")
        src = str(img.get("path") or img.get("url") or img.get("data") or "")
        if not src:
            continue
        out.append(ImageRef(
            kind=kind, src=src,
            media_type=str(img.get("media_type") or "image/png"),
            alt=str(img.get("alt_text") or ""),
        ))
    return out


def _message_acts(msg: Any, kind: str) -> list[Act]:
    """The acts carried by a step's ``message`` (str or ContentPart list)."""
    acts: list[Act] = []
    if isinstance(msg, str):
        if msg.strip():
            acts.append(Act(kind=kind, text=msg))
        return acts
    if not isinstance(msg, list):
        return acts
    # A multimodal message: keep text and images in the order they were sent.
    for part in msg:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "text" and (part.get("text") or "").strip():
            acts.append(Act(kind=kind, text=str(part["text"])))
        elif part.get("type") == "image":
            imgs = _images_from_content([part])
            if imgs:
                acts.append(Act(kind=kind, images=imgs))
    return acts


def _step_acts(step: dict[str, Any]) -> list[Act]:
    """Every act one trajectory step recorded, at full length and in order.

    Ordering follows how a model turn is actually produced — think, speak, call
    tools — then the observation a step of its own carries. Empty-but-present
    content (a tool that returned nothing, a thinking block with no text) is
    kept as an ``empty`` act, because "produced nothing" is a real observation
    and silently dropping it makes the trace look like the call never happened.
    """
    acts: list[Act] = []
    source = str(step.get("source") or "")

    reasoning = step.get("reasoning")
    if isinstance(reasoning, str) and reasoning.strip():
        acts.append(Act(kind="reason", text=reasoning))
    elif reasoning is not None and not str(reasoning or "").strip():
        # An empty thinking block still means the model was asked to think.
        acts.append(Act(kind="reason", empty=True))

    # ``message`` on an agent step is what it said; on environment/system steps
    # it is an env update or a framework note — labelled distinctly so the
    # timeline never implies the harness's words came from the model.
    msg_kind = {"agent": "note", "environment": "env", "system": "sys",
                "user": "note"}.get(source, "note")
    acts.extend(_message_acts(step.get("message"), msg_kind))

    for call in step.get("tool_calls") or []:
        if not isinstance(call, dict):
            continue
        args = call.get("arguments")
        args = args if isinstance(args, dict) else {}
        acts.append(Act(
            kind="tool",
            tool_name=str(call.get("name", "?")),
            tool_args=_json_args(call.get("arguments")),
            arguments=args,
            tool_call_id=str(call.get("id") or ""),
        ))

    obs = step.get("observation")
    if isinstance(obs, dict):
        for res in obs.get("results") or []:
            if not isinstance(res, dict):
                continue
            kind = "obs_err" if res.get("is_error") else "obs"
            call_id = str(res.get("tool_call_id") or "")
            content = res.get("content") or []
            texts = [cp for cp in content
                     if isinstance(cp, dict) and cp.get("type") == "text"]
            images = _images_from_content(content)
            for cp in texts:
                acts.append(Act(kind=kind, text=str(cp.get("text") or ""),
                                tool_call_id=call_id,
                                empty=not str(cp.get("text") or "").strip()))
            if images:
                acts.append(Act(kind=kind, images=images, tool_call_id=call_id))
            if not texts and not images:
                # The tool answered with no content at all.
                acts.append(Act(kind=kind, tool_call_id=call_id, empty=True))
        if obs.get("error"):
            acts.append(Act(kind="obs_err", text=str(obs["error"])))

    # A step that carried only structured metadata (a codex ``file_change``, an
    # env state update) still happened; surface it as an event act so its
    # ``extra`` is rendered rather than the step vanishing.
    if not acts:
        acts.append(Act(kind="event", empty=True))
    return acts


def _json_args(arguments: Any) -> str:
    """A tool call's arguments as JSON — complete, never clipped."""
    if arguments is None:
        return ""
    try:
        return json.dumps(arguments, indent=2, ensure_ascii=False, sort_keys=False)
    except (TypeError, ValueError):
        return str(arguments)


def _step_view(step: dict[str, Any]) -> StepView:
    """One raw trajectory step → a :class:`StepView` with envelope + acts."""
    m = step.get("metrics")
    metrics: Metrics | None = None
    if isinstance(m, dict):
        metrics = Metrics(
            input_tokens=_int(m.get("input_tokens")),
            output_tokens=_int(m.get("output_tokens")),
            cache_read_tokens=_int(m.get("cache_read_tokens")),
            cache_creation_tokens=_int(m.get("cache_creation_tokens")),
            cost_usd=m.get("cost_usd"),
        )
    extra = step.get("extra")
    return StepView(
        step_id=_int(step.get("step_id")),
        source=str(step.get("source") or "?"),
        timestamp=str(step.get("timestamp") or ""),
        acts=_step_acts(step),
        metrics=metrics if (metrics and metrics.has_data) else None,
        extra=dict(extra) if isinstance(extra, dict) else {},
    )


def _segment_steps(traj: dict[str, Any]) -> list[list[StepView]]:
    """Split a trajectory's steps into one :class:`StepView` list per turn.

    A new segment starts at each ``user`` step (the message the agent received);
    all subsequent non-user steps belong to that turn. The ``user`` step itself
    is NOT included — its text is the turn's ``incoming`` — except when it
    carries content beyond that text (an image, say), which would otherwise be
    lost.
    """
    segments: list[list[StepView]] = []
    for step in traj.get("steps") or []:
        if not isinstance(step, dict):
            continue
        if step.get("source") == "user":
            segments.append([])
            # The inbound text renders as the turn's `incoming`; anything else
            # the step carried (images, a non-text part) has no other home.
            if _images_from_content(step.get("message")):
                segments[-1].append(_step_view(step))
        else:
            if not segments:            # steps before any user step
                segments.append([])
            segments[-1].append(_step_view(step))
    return segments


def load_run(run_dir: str | Path) -> RunView:
    """Read a run directory (or any file inside one) into a :class:`RunView`."""
    p = Path(run_dir)
    if p.is_file():
        p = p.parent

    trajectories: dict[str, dict[str, Any]] = {}
    for tj in sorted(p.glob("trajectory_*.json")):
        role = tj.stem[len("trajectory_"):]
        data = _read_json(tj)
        if data:
            trajectories[role] = data
    return RunView(
        run_dir=p,
        run=_read_json(p / "run.json"),
        conversation=_read_json(p / "conversation.json"),
        eval=_read_json(p / "eval_result.json"),
        trajectories=trajectories,
        metrics_report=_read_json(p / "metrics.json"),
    )


def is_run_dir(path: Path) -> bool:
    """A directory is a run dir if it holds a ``run.json`` (or a conversation)."""
    return (path / "run.json").is_file() or (path / "conversation.json").is_file()


def iter_runs(root: str | Path) -> Iterator[RunView]:
    """Yield a :class:`RunView` for every run directory under ``root`` (sorted)."""
    root = Path(root)
    if is_run_dir(root):
        yield load_run(root)
        return
    seen: set[Path] = set()
    for marker in sorted(root.rglob("run.json")):
        rd = marker.parent
        if rd in seen:
            continue
        seen.add(rd)
        yield load_run(rd)
