"""TaskDriver — drives a task's setup/evaluate hooks against a sandbox.

Decoupled from provisioning: the constructor takes the already-acquired
:class:`SandboxHandle` from the environment layer. Task hooks are called as
``setup_fn(task_cfg, sandbox)`` / ``evaluate_fn(task_cfg, sandbox)`` — the
sandbox exposes ``run_command`` / ``read_file`` / ``write_file`` / ``exists``
etc. (see ``base_interface/sandbox.py``); hooks may be sync or async.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict

from ..base_interface import SandboxHandle

from .loader import TaskLoader

logger = logging.getLogger(__name__)


class TaskDriver:
    """Task setup + evaluate against an acquired sandbox.

    The sandbox is owned by the environment layer (the Provider produced it).
    TaskDriver does NOT release it — its ``close()`` method only resets its
    own reference; the env releases the sandbox in its own ``close_async``.
    """

    def __init__(
        self,
        task_path: str,
        sandbox: SandboxHandle,
        variant: int = 0,
        skip_setup: bool = False,
        os_type: str | None = None,
    ):
        self._variant = variant
        self._skip_setup = skip_setup

        self._task_loader = TaskLoader(task_path)
        self._task_info = self._task_loader.load(variant_index=variant)
        self._os_type = os_type or self._task_info.get("os_type", "linux")

        self._sandbox: SandboxHandle | None = sandbox

    async def setup(self) -> None:
        if self._skip_setup:
            logger.info("Skipping task setup (skip_setup=True)")
            return

        setup_fn = self._task_loader.get_setup_fn()
        if setup_fn is None:
            logger.info("No setup function found — skipping")
            return

        task_cfg = self._make_task_cfg()
        logger.info("Running task setup...")
        result = setup_fn(task_cfg, self._sandbox)
        if asyncio.iscoroutine(result):
            await result
        logger.info("Task setup completed")

    async def evaluate(self) -> Dict[str, Any]:
        evaluate_fn = self._task_loader.get_evaluate_fn()
        if evaluate_fn is None:
            logger.info("No evaluate function found — skipping")
            return {"score": None, "success": None}

        task_cfg = self._make_task_cfg()
        try:
            result = evaluate_fn(task_cfg, self._sandbox)
            if asyncio.iscoroutine(result):
                result = await result
        except Exception as e:
            logger.error("Evaluate failed: %s", e)
            return {"error": str(e), "score": None, "success": None}
        if isinstance(result, list):
            return {"score": result[0] if result else None, "raw_scores": result}
        if isinstance(result, dict):
            return result
        return {"score": result}

    async def close(self) -> None:
        """Drop the sandbox reference. The env owns sandbox lifecycle."""
        self._sandbox = None

    def _make_task_cfg(self):
        task_cfg = self._task_loader.build_task_cfg(variant_index=self._variant)
        if not hasattr(task_cfg, "metadata") or getattr(task_cfg, "metadata") is None:
            task_cfg.metadata = self._task_info.get("metadata", {})
        return task_cfg
