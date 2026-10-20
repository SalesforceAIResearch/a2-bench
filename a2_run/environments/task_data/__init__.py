"""Task data staging — pull input/software onto the sandbox, ready
reference for evaluation.

Three sources today, dispatched on yaml ``artifacts_path.task_data_source``:

  ``"baked_in_sandbox"``     image already has input/ + reference.7z baked in
  ``"gs://<bucket>"``        rsync from a GCS bucket
  ``"s3://<bucket>"``        sync from an S3 bucket
  ``"hf://<dataset>"``       HuggingFace Hub (STUB)
  ``"local:<host_dir>"``     docker cp from a HOST dir — for a data-less Docker
                             image: input before the agent, reference before eval

Regardless of source, ``input/`` becomes an agent-owned writable working copy
after staging. The host or remote source is never modified. ``reference/`` is
kept evaluator-owned and unavailable until evaluation.

Each backend module exposes two coroutines:

  ``async def stage_input(sandbox, task_data, *, source) -> dict``
  ``async def stage_reference(sandbox, task_data, *, source) -> dict``

The lifecycle calls :func:`select` to pick the module by scheme, then
invokes its two functions in the right phases.
"""
from __future__ import annotations

import shlex
from typing import Any, Protocol

from ...base_interface import SandboxHandle, TaskDataSpec


class _Backend(Protocol):
    async def stage_input(
        self, sandbox: SandboxHandle, task_data: TaskDataSpec, *, source: str,
        role: str | None = None,
    ) -> dict[str, Any]: ...

    async def stage_reference(
        self, sandbox: SandboxHandle, task_data: TaskDataSpec, *, source: str,
        role: str | None = None,
    ) -> dict[str, Any]: ...


def select(task_data_source: str):
    """Pick a backend by scheme of ``task_data_source``."""
    if task_data_source == "baked_in_sandbox":
        from . import baked_in_sandbox
        return baked_in_sandbox
    if task_data_source.startswith("local:"):
        from . import local_host
        return local_host
    raise ValueError(
        f"unknown task_data_source {task_data_source!r}: expected "
        f"'baked_in_sandbox' or 'local:<dir>'"
    )


# ============================================================================
# Shared helpers
# ============================================================================

def task_subdir(sandbox: SandboxHandle, task_data: TaskDataSpec) -> str:
    """Container-side data root — the OPAQUE, generic mount the agent sees.

    A container hosts exactly one role of one task-variant (fresh per unit,
    ``docker rm`` on release), so the ``<domain>/<task>/<variant>`` hierarchy
    that disambiguates the *host* corpus carries no information inside the box —
    it only leaks the task's identity and (for rotated-condition scenarios) the
    hidden variant (e.g. ``access_b`` = which peer holds egress) to an agent
    that runs ``ls`` on its data mount. We therefore stage into a flat
    ``<task_data_root>`` with generic ``input/ output/ reference/ software/``
    subdirs, reading like an ordinary data mount rather than a graded-eval
    layout. The host keeps its structured ``<domain>/<task>/<variant>[/<role>]``
    tree (see ``_host_task_dir``); only the destination inside the sandbox is
    flattened. ``task_data`` is retained for signature stability and future
    per-task overrides.
    """
    return sandbox.task_data_root.rstrip("/\\")


def join(sandbox: SandboxHandle, *parts: str) -> str:
    """OS-native path join. ``parts[0]`` is the root."""
    sep = "/" if sandbox.is_linux else "\\"
    head = parts[0].rstrip("/\\")
    tail = sep.join(p.strip("/\\") for p in parts[1:])
    return f"{head}{sep}{tail}" if tail else head


def shell_q(sandbox: SandboxHandle, path: str) -> str:
    """Path quoter — POSIX shlex on linux, single-quote literal on windows
    (PowerShell single-quoted strings have no interpolation)."""
    return shlex.quote(path) if sandbox.is_linux else f"'{path}'"


__all__ = ["select", "task_subdir", "join", "shell_q"]
