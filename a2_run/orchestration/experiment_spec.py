"""ExperimentSpec dataclasses — pure data, no IO.

Built by :mod:`a2_run.orchestration.config_loader` from a yaml file, consumed
by :class:`a2_run.orchestration.Runner`. One ExperimentSpec describes a batch:
which provider, which agents (inline configs), which tasks × variants,
and how to mirror artifacts.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class TaskSpec:
    """One task entry.

    ``variants`` selects which of the task's declared variants to run. Each
    entry is either a variant NAME (a string matching the task.yaml
    ``variants:`` list — preferred, and stable if the variant order changes) or
    a positional index (int, legacy). ``None`` — the experiment yaml omits the
    ``variants:`` key — runs EVERY declared variant. Names/indices are resolved
    against the task's declared variants in :meth:`Runner.enumerate_units`,
    which fails loud on an unknown name or out-of-range index.
    """

    path: str
    variants: list[int | str] | None = None


@dataclass
class AgentSpec:
    """One agent entry. Multiple AgentSpecs in an experiment ⇒ matrix.

    Attributes:
        id: short label naming the agent-level folder in the output tree.
            Defaults to the harness name (``class_``); set an explicit ``id``
            to run multiple config-variants of the same class side by side.
        class_: either a shortcut (``"claude_code"``) registered in
            ``a2_run.orchestration.factory.AGENT_REGISTRY``, or a fully-qualified
            Deployer class path (e.g. ``"my_pkg.MyDeployer"``).
        config: kwargs passed verbatim into the deployer's Config dataclass.
        executor: which substrate to run the deployer in: ``"sandbox"``
            (inside the sandbox container) or ``"local"`` (this Python
            process). Must be in ``DeployerCls.supported_executors`` or
            ``None`` (factory uses ``DeployerCls.default_executor``).
    """

    id: str
    class_: str
    config: dict[str, Any] = field(default_factory=dict)
    executor: str | None = None


@dataclass
class ProviderSpec:
    """VM provider selection. ``kind`` picks the impl; ``config`` is its kwargs."""

    kind: str                             # docker | (stub for tests)
    config: dict[str, Any] = field(default_factory=dict)


@dataclass
class EnvironmentSpec:
    """Resolved environment: which provider instantiates each task snapshot.

    provider is chosen PER SNAPSHOT, so one environment can mix backends.

    * ``provider_specs`` — one :class:`ProviderSpec` per provider *kind* that
      any snapshot routes to (``docker`` carries the resolved image +
      sizing). Built lazily into provider instances by the runner.
    * ``snapshot_kind`` — maps a task-card snapshot tag to the provider kind
      that serves it (the per-snapshot ``provider:`` in the env yaml).
    * ``default_kind`` — for single-provider environments (no per-snapshot
      map) every snapshot routes here.
    """

    provider_specs: dict[str, ProviderSpec] = field(default_factory=dict)
    snapshot_kind: dict[str, str] = field(default_factory=dict)
    default_kind: str | None = None

    def kind_for(self, snapshot: str) -> str:
        kind = self.snapshot_kind.get(snapshot) or self.default_kind
        if kind is None:
            raise KeyError(
                f"snapshot {snapshot!r} is not mapped to any provider in the "
                f"environment (mapped: {sorted(self.snapshot_kind)})"
            )
        return kind


@dataclass
class ArtifactsSpec:
    """Artifact path config — yaml ``artifacts:`` block.

    ``task_data_source``: ``"baked_in_sandbox"`` (image already has the task
    data) or ``"local:<host_dir>"`` (staged from a host dir).

    ``output_path``: ``None`` (yaml ``null``) skips the output gather —
    files are lost on teardown; ``"local"`` pulls the env's output dir to
    ``<run_dir>/output/``.
    """

    task_data_source: str = "baked_in_sandbox"
    output_path: str | None = None


@dataclass
class OutputSpec:
    """Where the on-disk run dirs go."""

    root: str = ".logs/a2"


@dataclass
class A2ASpec:
    """Run-level knobs for agent-to-agent (pair) mode — yaml ``a2a:`` block.

    Present (possibly all-default) exactly when the experiment yaml's
    ``agents:`` is a role→preset mapping.
    """

    max_messages: int = 16
    """Maximum messages (hops) recorded across the whole conversation, all
    senders combined. Replaces the old ``max_rounds`` (full exchanges):
    with N agents there is no global alternation for "round" to mean —
    one message = one hop is the unit that stays well-defined."""

    channel: str = "a2a"
    """Which :data:`a2_run.protocols.CHANNEL_REGISTRY` entry delivers messages
    — the protocol variable. ``a2a`` = the A2A v1.0 ``Message`` envelope;
    ``direct`` = a bare ``from: <name>`` line (pre-protocol baseline). Same
    body, same wire, so a pair of runs isolates the envelope."""


@dataclass
class ExperimentSpec:
    name: str
    output: OutputSpec
    environment: EnvironmentSpec
    tasks: list[TaskSpec]
    artifacts: ArtifactsSpec = field(default_factory=ArtifactsSpec)

    concurrency: int = 1
    """Max units running simultaneously. ``1`` = sequential. Each unit
    holds a slot for its full lifetime — VM acquire + agent run + post-
    launch fan-out + eval — so the cap is effectively "max VMs alive at
    once". Size to the LLM rate limit and host capacity."""

    cleanup_mode: str = "delete"
    """VM disposition after a unit finishes.

    - ``"delete"`` (default): tear the container down.
    - ``"stop"``: stop the container; it remains so a later run can
      re-start it and inspect agent artifacts.
    - ``"keep"``: leave the container running (debug / reproducer use).
    """

    wall_time_s: int | None = None
    """Experiment-wide agent wall-clock budget (seconds), overriding each
    task card's ``vm.timeout``. ``None`` (default) ⇒ use the per-task value
    (or the framework default). Set via the top-level ``wall_time_s:`` yaml
    key — e.g. ``18000`` for a uniform 5h budget across all tasks. In a2a
    mode this bounds the WHOLE conversation (all turns of both agents)."""

    roles: dict[str, AgentSpec] | None = None
    """Role name → agent preset, from the mapping-form ``agents:`` in the
    experiment yaml. Role names must match the task's declared roles."""

    a2a: A2ASpec | None = None
    """A2a run knobs. Set (with defaults) whenever ``roles`` is set."""


# =============================================================================
# Derived run units (one per task × variant combination)
# =============================================================================

@dataclass
class PairRunUnit:
    """One concrete a2a (two-agent, one task, one variant) tuple to execute.

    ``roles`` preserves the experiment yaml's mapping order; the identity
    strings feed the same RunWriter slug machinery as single-agent units.
    """

    roles: dict[str, AgentSpec]
    task_path: str
    variant_index: int
    variant_name: str = ""

    @property
    def agent_id(self) -> str:
        """Composite identity, e.g. ``asker=claude_code__oracle=claude_code``."""
        return "__".join(f"{role}={spec.id}" for role, spec in self.roles.items())

    @property
    def model_id(self) -> str:
        """Composite model string for the output-tree model level."""
        return "__".join(
            str(spec.config.get("model") or "unknown")
            for spec in self.roles.values()
        )

    @property
    def slug(self) -> str:
        from .run_writer import slug_variant

        return (
            f"{self.agent_id}/{self.task_path}/"
            f"{slug_variant(self.variant_name, self.variant_index)}"
        )


@dataclass
class UnitResult:
    """Per-unit outcome. No aggregation; the Runner returns ``list[UnitResult]``."""

    unit: PairRunUnit
    status: str                                  # completed | failed | cancelled | not_executed
    score: float | None = None
    eval_status: str = "not_executed"
    duration_s: float | None = None
    run_dir: Path | None = None
    error: str | None = None
