"""Template a2a task — the scoring code half of a task.

Copy this directory to ``tasks/<domain>/<task>/``, edit the sibling
``task.yaml`` for all declarative setup (identity, vm, briefs, agent cards,
paths), and implement the two hooks below. Keep this file to the scoring
logic only — anything declarative belongs in ``task.yaml``.

Contract the framework relies on:
  * ``load()`` returns one ``Task`` per declared variant — always
    ``return load_tasks(__file__)``; the builder reads ``task.yaml``.
  * ``start(task_cfg, sandboxes)`` runs before any turn. For an a2a task
    ``sandboxes`` is a ``dict`` role → SandboxHandle (each role's world can
    differ). Assert staged inputs landed and ready the output dirs.
  * ``evaluate(task_cfg, sandboxes)`` runs after the conversation (and after
    reference/ is staged) and returns ``list[float]`` (the first element is
    the score). Read paths from ``cfg = task_config(__file__)``.

Scoring guidance:
  * Keep pure scoring logic in module-level functions (host-side, stdlib) so
    it is offline-testable against synthetic filings.
  * If you must touch large binaries or a DB, run a self-contained python
    snippet INSIDE the sandbox and parse one JSON line on stdout — the eval
    host should stay pure-stdlib (see mlinfra/procurement/dataops for the
    established pattern).
"""
from __future__ import annotations

import logging

from tasks.task_spec import load_tasks, task_config

logger = logging.getLogger(__name__)


def load():
    return load_tasks(__file__)


async def start(task_cfg, sandboxes):
    """Assert staged inputs are present; ready each role's output dir."""
    cfg = task_config(__file__)
    for role in ("role_a", "role_b"):
        sb = sandboxes[role]
        # Example spot-check — replace with this role's real staged inputs.
        # if not await sb.exists(f"{cfg.input_dir}/<file>"):
        #     raise RuntimeError(f"staged input missing in {role} sandbox")
        await sb.mkdir(cfg.remote_output_dir)
    # Clear any stale deliverable from a prior run:
    # await sandboxes["role_a"].rm([cfg.deliverable_path])
    logger.info("[template] output dirs ready")


async def evaluate(task_cfg, sandboxes) -> list[float]:
    """Score the filed artifacts. Return [score] in [0.0, 1.0]."""
    cfg = task_config(__file__)
    # Example: read a deliverable and compare against the staged reference.
    # deliverable = await sandboxes["role_a"].read_text(cfg.deliverable_path)
    # expected = await sandboxes["role_a"].read_text(cfg.reference_path)
    # return [1.0 if deliverable.strip() == expected.strip() else 0.0]
    _ = cfg  # silence lint until implemented
    raise NotImplementedError("implement evaluate() for this task")
