"""BaseAgentDeployer — the minimal contract every a2-bench agent implements.

A deployer is a few Python methods the framework places into an executor
(sandbox / local). The framework calls ``install`` once, then ``launch_turn``
once per SESSION: normally a single long-lived session per role (blocking
``a2a_send_message`` keeps it alive for the whole conversation), plus one per
§3.4.3 resume. The pair lifecycle builds each role's trajectory host-side
from the gathered session transcripts.

Lives in ``base_interface/`` (not ``agents/``) so concrete subclasses can
import the contract without dragging in the agents package.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar


if TYPE_CHECKING:
    # BaseExecutor and BaseAgentDeployer reference each other in their
    # public signatures. Same-package TYPE_CHECKING keeps the cycle from
    # surfacing at runtime; type-checkers see both directions.
    from .executor import BaseExecutor


# =============================================================================
# Config
# =============================================================================
#
# There is intentionally NO shared ``BaseAgentConfig``: each agent's config is
# a standalone ``@dataclass`` in ``a2_run.agents.<agent>.config`` declaring
# only the knobs that deployer consumes (plus ``model`` and a ``name``
# ClassVar). The episode wall budget is orchestration-owned (derived from the
# task, enforced by the executor's ``asyncio.wait_for``); deployers do not
# self-poll a timeout — on ``CancelledError`` they reap their child and
# re-raise.


# =============================================================================
# Run + episode results
# =============================================================================

@dataclass
class AgentRunResult:
    """Outcome of one :meth:`BaseAgentDeployer.launch_turn` session.

    Pure data; serializable across executor boundaries. The inter-agent wire
    is the hub, so nothing here carries the conversation — the fields are
    lifecycle status plus diagnostics.
    """

    status: str                          # "completed" | "timeout" | "failed"
    transcript_path: str | None = None
    stderr_path: str | None = None
    pid: int | None = None
    exit_code: int | None = None
    duration_s: float | None = None
    error: str | None = None

    # ---- diagnostics (populated by launch_turn implementations) ----
    reply_text: str | None = None
    """The session's final output text. Diagnostics only — never delivered
    to the peer (the wire is the hub)."""
    session_id: str | None = None
    """Harness-native session/thread id for this role. Diagnostics only;
    resume state lives in the role's work_dir."""


# =============================================================================
# Deployer ABC
# =============================================================================

class BaseAgentDeployer(abc.ABC):
    """Minimal deployer contract.

    Subclasses MUST set :attr:`supported_executors` (declares which
    substrates this agent can run on: any subset of ``{"sandbox","local"}``)
    AND :attr:`default_executor` (the one used when yaml omits the field).
    The framework validates yaml ``executor`` against ``supported_executors``.
    """

    default_executor: ClassVar[str] = ""
    """The executor type used when yaml's ``agent.executor`` is omitted.
    Empty = error at resolve time. Concrete deployer subclass declares."""

    supported_executors: ClassVar[frozenset[str]] = frozenset()
    """Subclass overrides — strings match yaml ``executor: <type>`` values
    (and :attr:`BaseExecutor.type` class attribute on the concrete impl).
    Empty set is a programmer error caught at ``resolve_agent`` time."""

    supports_a2a: ClassVar[bool] = False
    """True when the deployer implements :meth:`launch_turn`. Required for
    both roles of a pair (a2a) run."""

    supports_session_resume: ClassVar[bool] = False
    """True when :meth:`launch_turn` accepts ``turn_index > 0`` and resumes the
    role's EXISTING model thread rather than starting a blank one.

    Lets the conversation driver honour A2A's follow-up pattern (spec §3.4.3):
    a role that ended without sending an addressed message can be re-asked, and
    the re-ask continues where it left off. Requires per-turn artifact names
    (so an earlier transcript isn't overwritten) and a durable thread id.

    False (the default) means a departed role stays gone. Declared rather than
    probed, because guessing wrong silently corrupts a role's telemetry."""

    def __init__(self, executor: BaseExecutor):
        self.executor = executor
        self.config = executor.config        # convenience alias

    # ---- abstract methods ----

    @abc.abstractmethod
    async def install(self) -> None:
        """Stage prereqs for this run. Use ``self.executor`` for all
        substrate I/O; the substrate itself (VM, container, host
        process) is the framework's concern — the agent code is
        identical anywhere."""

    # ---- optional a2a turn interface ----

    async def launch_turn(
        self, message: str, *, turn_index: int, standing_context: str | None = None,
    ) -> AgentRunResult:
        """Run ONE session for this role: feed *message*, wait for it to end.

        ``turn_index`` counts this role's sessions: 0 opens the conversation;
        a later index resumes the role's existing thread (state persists in
        ``work_dir`` — a fresh deployer instance is constructed per session,
        so nothing may live on ``self``). ``standing_context`` carries the
        agent's identity + environment config; harness-backed deployers
        install it as that harness's native standing-instruction file (e.g.
        ``CLAUDE.md``), refreshed on every session so a resume never runs
        without it. Only called when :attr:`supports_a2a` is True."""
        raise NotImplementedError(
            f"{type(self).__name__} does not support a2a sessions"
        )

    # ---- optional metadata ----

    @property
    def version(self) -> str | None:
        """CLI / SDK version string surfaced in run.json + trajectory.agent.version.
        Override if the agent has a meaningful version pin."""
        return None
