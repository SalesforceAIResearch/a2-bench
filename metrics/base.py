"""Metric interface + offline runner for a2a interaction metrics.

A task's ``evaluate()`` scores its deliverable and is task-specific. A Metric
scores the *interaction* per agent, offline from a finished run dir. Most are
task-agnostic; converted tasks may also seal a versioned scenario condition for
deterministic capability/delegation measurements. Adding a metric never means
re-running an experiment.

    @register
    class MyMetric(Metric):
        name = "my_metric"
        async def compute(self, run): return self.result(scores={"x": 1.0})
"""
from __future__ import annotations

import asyncio
import importlib
import json
import logging
import time
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, ClassVar, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field

from a2_run.transcript.loader import RunView

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "a2-metrics-v1"

# Imported by load_builtins() so metrics self-register. `metrics` is a PEP 420
# namespace package (like a2_run / tasks), so registration is an explicit import.
_BUILTIN_MODULES = ("metrics.judge", "metrics.collusion", "metrics.grounding",
                    "metrics.integrity", "metrics.leak", "metrics.write_grounding",
                    "metrics.delegation")

MetricStatus = Literal["ok", "skipped", "error"]


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


# =============================================================================
# Result shapes
# =============================================================================

class Citation(BaseModel):
    """A quote from the transcript backing a judge's claim.

    ``resolved`` is False when the quote was not found in the recorded messages
    — kept as a reviewer pointer, but it is not evidence.
    """

    model_config = ConfigDict(extra="forbid")

    index: int | None = None       # 1-based message index the judge cited
    role: str = ""                 # who authored the cited message
    quote: str = ""
    description: str = ""
    resolved: bool = False


class RoleScore(BaseModel):
    """One agent's dimension scores plus the prose behind them."""

    model_config = ConfigDict(extra="forbid")

    scores: dict[str, int | float | None] = Field(default_factory=dict)
    summary: str = ""
    justification: str = ""
    citations: list[Citation] = Field(default_factory=list)


class MetricResult(BaseModel):
    """One metric's output for one run."""

    model_config = ConfigDict(extra="forbid")

    name: str
    level: int | None = None       # 1 = outcome/resource, 2 = interaction
    status: MetricStatus = "ok"
    scores: dict[str, int | float | None] = Field(default_factory=dict)
    """Run-level rollups; per-agent numbers live in :attr:`roles`."""
    roles: dict[str, RoleScore] = Field(default_factory=dict)
    summary: str = ""
    error: str | None = None
    duration_s: float | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class MetricReport(BaseModel):
    """Every metric computed for one run — serialised to ``metrics.json``."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = SCHEMA_VERSION
    run_id: str = ""
    run_dir: str = ""
    task_path: str = ""
    computed_at: str = Field(default_factory=_now_iso)
    results: dict[str, MetricResult] = Field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return all(r.status == "ok" for r in self.results.values())


# =============================================================================
# Interface
# =============================================================================

class Metric(ABC):
    """Base class for an offline, per-agent interaction metric."""

    name: ClassVar[str] = ""
    level: ClassVar[int] = 2
    description: ClassVar[str] = ""

    @abstractmethod
    async def compute(self, run: RunView) -> MetricResult:
        """Score one finished run."""

    def result(
        self,
        *,
        scores: dict[str, int | float | None] | None = None,
        roles: dict[str, RoleScore] | None = None,
        summary: str = "",
        status: MetricStatus = "ok",
        error: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> MetricResult:
        return MetricResult(
            name=self.name, level=self.level, status=status,
            scores=dict(scores or {}), roles=dict(roles or {}),
            summary=summary, error=error, metadata=dict(metadata or {}),
        )

    def skip(self, reason: str) -> MetricResult:
        """Not scoreable (no messages, no judge creds, ...) — not an error."""
        return self.result(status="skipped", summary=reason, error=reason)


# =============================================================================
# Registry
# =============================================================================

_REGISTRY: dict[str, type[Metric]] = {}


def register(cls: type[Metric]) -> type[Metric]:
    if not cls.name:
        raise ValueError(f"{cls.__name__} must set a non-empty `name`")
    _REGISTRY[cls.name] = cls
    return cls


def load_builtins() -> None:
    for module in _BUILTIN_MODULES:
        importlib.import_module(module)


def metric_class(name: str) -> type[Metric]:
    load_builtins()
    if name not in _REGISTRY:
        raise KeyError(
            f"unknown metric {name!r}; available: {', '.join(available_metrics())}"
        )
    return _REGISTRY[name]


def get_metric(name: str, **kwargs: Any) -> Metric:
    return metric_class(name)(**kwargs)


def available_metrics() -> list[str]:
    load_builtins()
    return sorted(_REGISTRY)


# =============================================================================
# Runner + persistence
# =============================================================================

async def run_metrics(
    run: RunView, metrics: Sequence[Metric], *, concurrency: int = 4,
) -> MetricReport:
    """Compute metrics for one run. A metric that raises lands as status=error
    rather than voiding the report."""
    sem = asyncio.Semaphore(max(1, concurrency))

    async def one(m: Metric) -> MetricResult:
        started = time.monotonic()
        async with sem:
            try:
                res = await m.compute(run)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                logger.exception("metric %s failed for %s", m.name, run.run_dir)
                res = m.result(status="error", summary=f"{type(e).__name__}: {e}",
                               error=f"{type(e).__name__}: {e}")
        res.duration_s = round(time.monotonic() - started, 3)
        return res

    results = await asyncio.gather(*(one(m) for m in metrics))
    return MetricReport(
        run_id=run.run_id, run_dir=str(run.run_dir), task_path=run.task_path,
        results={r.name: r for r in results},
    )


def write_metrics(
    report: MetricReport, run_dir: str | Path | None = None, *, merge: bool = True,
) -> Path:
    """Write ``<run_dir>/metrics.json``. With ``merge`` (default), metrics absent
    from this report are preserved, so re-scoring one doesn't drop the others."""
    path = Path(run_dir if run_dir is not None else report.run_dir) / "metrics.json"

    payload = report
    if merge and path.is_file():
        try:
            prior = MetricReport.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            logger.warning("metrics: ignoring unreadable %s: %s", path, e)
        else:
            merged = {**prior.results, **report.results}
            payload = report.model_copy(update={"results": merged})

    path.write_text(
        json.dumps(payload.model_dump(mode="json"), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def read_metrics(run_dir: str | Path) -> MetricReport | None:
    path = Path(run_dir) / "metrics.json"
    try:
        return MetricReport.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        if path.exists():
            logger.warning("metrics: could not parse %s: %s", path, e)
        return None
