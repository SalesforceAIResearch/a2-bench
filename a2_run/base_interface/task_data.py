"""Task-side data shapes.

Currently just :class:`TaskDataSpec` — the contract describing what
input/reference data a task needs staged into the VM. Read by
:mod:`a2_run.environments.data_staging` to drive ``stage_input`` /
``stage_reference``.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class TaskDataSpec:
    """Where a task's data lives and what to stage where.

    Populated by ``TaskLoader`` from the task module metadata (sourced from
    ``task.yaml``). Consumed by ``data_staging`` (pushes into the VM) and
    indirectly by the orchestrator (decides whether to fetch a GCS
    prefix, etc.). ``input/`` is an agent-owned working copy and is always
    writable inside the sandbox. ``reference/`` remains evaluator-owned and
    is staged only after the agent finishes.
    """

    requires_task_data: bool = False
    domain_name: str | None = None
    task_name: str | None = None
    variant_name: str | None = None
    source_relpath: str | None = None
    input_dir: str | None = None
    software_dir: str | None = None
    reference_dir: str | None = None
    reference_gcs_prefix: str | None = None
    remote_output_dir: str | None = None
