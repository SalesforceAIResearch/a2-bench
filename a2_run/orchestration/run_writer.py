"""RunWriter: owns one run's directory + events.jsonl + finalize files.

LOG_SPEC.md is the source of truth; this class is its only writer. Layout:

    <output_root>/<agent_id>/<model_slug>/<task_slug>/<variant>/<YYYYMMDD_HHMMSS>/
        events.jsonl       append-only, fsync per line
        hops.jsonl         one routed conversation hop per line, streamed as
                           it happens (a2a runs) — the transcript a killed
                           run keeps; conversation.json is its finalize form
        run.json           schema_version=2, written once at finalize
        trajectory_<role>.json   ATIF-v1.0, one per role
        eval_result.json   {eval_status, score, eval_duration_s, error,
                            breakdown?}   breakdown = the task's rubric detail
        scenario.json      compact conversion condition (converted tasks only)
        origin_log/<agent_name>/    deployer work_dir pulled from VM
        output/                     agent output, when output_path="local"

The constructor refuses to overwrite an existing run dir
(``FileExistsError``). Each finalize write is wrapped in try/except; the
``events.jsonl`` is the authoritative trace even if one of the other writes
fails.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ----------------------------------------------------------------- slugs

_SLUG_RE = re.compile(r"[^a-z0-9-]+")


def slug_model(model: str) -> str:
    if not model:
        return "unknown-model"
    s = model.lower().replace(".", "-").replace("/", "-").replace("_", "-")
    s = _SLUG_RE.sub("-", s).strip("-")
    return s or "unknown-model"


def slug_task(task_path: str) -> str:
    return task_path.strip("/").replace("/", "__")


def slug_agent(agent_name: str) -> str:
    s = (agent_name or "unknown").lower().replace("-", "_")
    return re.sub(r"[^a-z0-9_]+", "_", s).strip("_") or "unknown"


def slug_variant(variant_name: str, variant_index: int) -> str:
    """Directory segment for a task variant."""
    if not variant_name:
        return f"v{variant_index}"
    s = variant_name.lower().replace("/", "-").replace(" ", "-")
    s = re.sub(r"[^a-z0-9._-]+", "-", s).strip("-._")
    return s or f"v{variant_index}"


def build_run_id(
    *,
    agent_id: str,
    model: str,
    task_path: str,
    variant_index: int,
    variant_name: str = "",
    ts: str,
) -> str:
    return (
        f"{slug_agent(agent_id)}__{slug_model(model)}__"
        f"{slug_task(task_path)}__{slug_variant(variant_name, variant_index)}__{ts}"
    )


class RunWriter:
    def __init__(
        self,
        *,
        output_root: Path,
        agent_id: str,
        model: str,
        task_path: str,
        variant_index: int,
        variant_name: str = "",
    ):
        self._ts = time.strftime("%Y%m%d_%H%M%S", time.gmtime())
        self._slug_agent = slug_agent(agent_id)
        self._slug_model = slug_model(model)
        self._slug_task = slug_task(task_path)
        self._variant_index = variant_index
        self._variant_name = variant_name
        self._slug_variant = slug_variant(variant_name, variant_index)

        self._run_dir = (
            output_root
            / self._slug_agent
            / self._slug_model
            / self._slug_task
            / self._slug_variant
            / self._ts
        )
        # Refuse to overwrite — LOG_SPEC §1 collision policy.
        if self._run_dir.exists():
            raise FileExistsError(f"run dir already exists: {self._run_dir}")
        self._run_dir.mkdir(parents=True, exist_ok=False)
        (self._run_dir / "origin_log").mkdir(parents=True, exist_ok=True)
        (self._run_dir / "output").mkdir(parents=True, exist_ok=True)

        self._run_id = build_run_id(
            agent_id=agent_id,
            model=model,
            task_path=task_path,
            variant_index=variant_index,
            variant_name=variant_name,
            ts=self._ts,
        )

        self._events_path = self._run_dir / "events.jsonl"
        # Line-buffered append; fsync after each write for SIGTERM safety.
        self._events_fh = self._events_path.open("a", buffering=1, encoding="utf-8")
        self._hops_fh = None  # lazily opened on the first a2a hop

    # ------------------------------------------------------------------ props

    @property
    def run_id(self) -> str:
        return self._run_id

    @property
    def run_dir(self) -> Path:
        return self._run_dir

    @property
    def variant_name(self) -> str:
        return self._variant_name
    # ----------------------------------------------------------------- events

    def emit_event(self, event_type: str, **data: Any) -> None:
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "type": event_type,
            "run_id": self._run_id,
        }
        if data:
            payload["data"] = data
        line = json.dumps(payload, default=str, ensure_ascii=False)
        try:
            self._events_fh.write(line + "\n")
            self._events_fh.flush()
            os.fsync(self._events_fh.fileno())
        except (OSError, ValueError) as e:
            # ValueError when fh is closed; OSError on fsync against a bad fd.
            logger.warning("emit_event(%s) failed: %s", event_type, e)

    def emit_hop(self, entry: dict[str, Any]) -> None:
        """Append one routed conversation hop to ``hops.jsonl``.

        Same durability contract as events.jsonl (fsync per line): a run
        killed at hop N keeps hops 1..N on disk even though the finalize-time
        conversation.json never gets written.
        """
        if self._hops_fh is None:
            self._hops_fh = (self._run_dir / "hops.jsonl").open(
                "a", buffering=1, encoding="utf-8"
            )
        line = json.dumps(entry, default=str, ensure_ascii=False)
        try:
            self._hops_fh.write(line + "\n")
            self._hops_fh.flush()
            os.fsync(self._hops_fh.fileno())
        except (OSError, ValueError) as e:
            logger.warning("emit_hop failed: %s", e)

    # --------------------------------------------------------------- finalize

    def write_run_json(self, meta: dict[str, Any]) -> None:
        path = self._run_dir / "run.json"
        try:
            path.write_text(
                json.dumps(meta, indent=2, ensure_ascii=False, default=str),
                encoding="utf-8",
            )
        except OSError as e:
            logger.warning("write_run_json failed: %s", e)

    def write_scenario(self, manifest: dict[str, Any]) -> None:
        """Persist a converted task's host-side experimental condition.

        The condition includes principals, capability ownership, trace
        evidence, and optional protected-value matchers. Resource inventories
        remain in the corpus registry. This file is never staged into an agent
        sandbox; writing it beside the trace keeps offline interaction metrics
        reproducible after the source checkout is gone.
        """
        path = self._run_dir / "scenario.json"
        try:
            path.write_text(
                json.dumps(manifest, indent=2, ensure_ascii=False, sort_keys=True),
                encoding="utf-8",
            )
        except OSError as e:
            logger.warning("write_scenario failed: %s", e)

    def write_trajectory(self, traj: Any, *, filename: str) -> None:
        # One trajectory per role: filename="trajectory_<role>.json".
        path = self._run_dir / filename
        try:
            blob = traj.model_dump_json(indent=2)
        except AttributeError:
            blob = json.dumps(traj, indent=2, ensure_ascii=False, default=str)
        try:
            path.write_text(blob, encoding="utf-8")
        except OSError as e:
            logger.warning("write_trajectory failed: %s", e)

    def write_eval_result(
        self,
        *,
        eval_status: str,
        score: float | None,
        eval_duration_s: float | None,
        error: dict[str, Any] | None,
        breakdown: dict[str, Any] | None = None,
    ) -> None:
        """Write ``eval_result.json``.

        ``breakdown`` is the task's own account of HOW the score was reached —
        the per-component subscores and per-criterion pass/fail that
        ``evaluate()`` returns under a ``breakdown`` key. It is optional and
        purely additive: a task that reports none writes the same four fields as
        before, and the viewer's score drill-down degrades to the bare score.
        See :mod:`a2_run.transcript.breakdown` for the shape it expects.
        """
        path = self._run_dir / "eval_result.json"
        payload = {
            "eval_status": eval_status,
            "score": score,
            "eval_duration_s": eval_duration_s,
            "error": error,
        }
        if breakdown:
            payload["breakdown"] = breakdown
        try:
            path.write_text(
                json.dumps(payload, indent=2, ensure_ascii=False, default=str),
                encoding="utf-8",
            )
        except OSError as e:
            logger.warning("write_eval_result failed: %s", e)

    def close(self) -> None:
        for fh in (self._events_fh, self._hops_fh):
            if fh is None:
                continue
            try:
                fh.close()
            except OSError:
                pass
