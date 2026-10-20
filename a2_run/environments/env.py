"""A2Env — wrapper around a Provider + SandboxHandle.

The benchmark drives the tested agent natively inside the environment, not via
(action, observation) pairs from the orchestrator. Orchestration accesses
``.sandbox`` / ``.current_phase`` directly.

Lifecycle::

    env = A2Env(provider=p, spec=env_spec)
    await env.reset_async()                 # acquires the sandbox
    # ... orchestrator uses env.sandbox, env.current_phase ...
    await env.close_async(mode="delete")    # releases the sandbox
"""

from __future__ import annotations

import logging

from ..base_interface import SandboxSpec, Provider, ReleaseMode, SandboxHandle

logger = logging.getLogger(__name__)


# LOG_SPEC §4 termination.phase values used by this module; the lifecycle
# passes its own phase strings to set_phase() directly.
PHASE_ENV_START = "env_start"
PHASE_CLEANUP = "cleanup"


class A2Env:
    """One env per (task × variant × agent). SandboxHandle is owned for the env's lifetime."""

    def __init__(self, *, provider: Provider, spec: SandboxSpec):
        self._provider = provider
        self._spec = spec
        self._sandbox: SandboxHandle | None = None
        self._current_phase: str | None = None

    # -------------------------------------------------------------------- props

    @property
    def spec(self) -> SandboxSpec:
        return self._spec

    @property
    def sandbox(self) -> SandboxHandle:
        if self._sandbox is None:
            raise RuntimeError("env.sandbox accessed before reset_async()")
        return self._sandbox

    @property
    def current_phase(self) -> str | None:
        return self._current_phase

    def set_phase(self, phase: str) -> None:
        self._current_phase = phase

    # -------------------------------------------------------------------- API

    async def reset_async(self) -> None:
        self._current_phase = PHASE_ENV_START
        self._sandbox = await self._provider.acquire(self._spec)

    async def close_async(self, mode: ReleaseMode = "delete") -> None:
        self._current_phase = PHASE_CLEANUP
        if self._sandbox is not None:
            try:
                await self._provider.release(self._sandbox, mode=mode)
            except Exception as e:
                logger.warning("provider.release failed for %s: %s", self._sandbox.id, e)
        self._sandbox = None
