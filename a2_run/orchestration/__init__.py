"""Orchestration: the only layer that knows about Agents, Environments, and Tasks.

The Runner enumerates ``PairRunUnit``s from an ``ExperimentSpec``
(yaml-built) and drives each through :func:`pair_lifecycle.run_pair_unit`,
which writes the per-run files via :class:`run_writer.RunWriter`.
"""

from .runner import Runner
from .experiment_spec import (
    AgentSpec,
    ArtifactsSpec,
    EnvironmentSpec,
    ExperimentSpec,
    OutputSpec,
    PairRunUnit,
    ProviderSpec,
    TaskSpec,
    UnitResult,
)

__all__ = [
    "AgentSpec",
    "ArtifactsSpec",
    "EnvironmentSpec",
    "ExperimentSpec",
    "OutputSpec",
    "PairRunUnit",
    "ProviderSpec",
    "Runner",
    "TaskSpec",
    "UnitResult",
]
