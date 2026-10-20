"""Executor — substrate adapter that hosts the deployer code and exposes
uniform I/O.

Two concrete subclasses:

* :class:`SandboxExecutor` — host-side adapter: ships the a2_run source
  into the sandbox container, spawns the in-container entry, polls until
  done. The deployer itself runs in-container under a LocalExecutor.
* :class:`LocalExecutor` — deployer + I/O both run in this Python
  process. Used by host-side deployers (dummy) and by the in-container
  entry.

Each Executor is the per-unit context the deployer reads (work_dir,
sandbox, env, config) AND the I/O + process-lifecycle surface it
acts through. The lifecycle constructs one Executor per run and
threads it into the deployer's ``__init__``.

Concrete deployers should depend on :class:`BaseExecutor` (the API
surface) rather than a specific subclass.
"""
from __future__ import annotations

from ..base_interface import BaseExecutor
from .local import LocalExecutor
from .sandbox import SandboxExecutor

# yaml ``executor: <type>`` → concrete Executor class.
EXECUTOR_REGISTRY: dict[str, type[BaseExecutor]] = {
    SandboxExecutor.type: SandboxExecutor,
    LocalExecutor.type: LocalExecutor,
}

__all__ = [
    "BaseExecutor",
    "EXECUTOR_REGISTRY",
    "LocalExecutor",
    "SandboxExecutor",
]
