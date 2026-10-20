"""Runner: yaml-described a2a experiment → concurrent run units.

Per-unit isolation: one fresh sandbox + one fresh deployer
instance per role, per unit.

Concurrency is a single ``asyncio.Semaphore`` sized to ``spec.concurrency``.
Each unit holds the slot for its full lifetime — sandbox acquire + relay
loop + eval — and boots one container per role, so size the cap
accordingly.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Iterable

from .factory import EnvironmentRouter
from .experiment_spec import ExperimentSpec, PairRunUnit, UnitResult

logger = logging.getLogger(__name__)


def _select_variants(
    requested: list[int | str] | None,
    declared: list[str],
    task_path: str,
) -> list[tuple[int, str]]:
    """Resolve an experiment's requested variants to ``(index, name)`` pairs.

    ``requested`` entries are declared variant NAMES (str) or positional
    indices (int); ``None`` (the ``variants:`` key omitted) selects EVERY
    declared variant. ``declared`` is the task's ordered variant-name list
    (from :meth:`TaskLoader.variant_names`).

    Fails loud on an unknown name or an out-of-range index — a mistyped variant
    must never silently fall back to variant 0.
    """
    n = len(declared)
    if requested is None:
        # No filter given ⇒ run everything the task declares. The empty-declared
        # case (a task with no load()) still yields the single default variant.
        return [(i, declared[i]) for i in range(n)] if n else [(0, "")]

    out: list[tuple[int, str]] = []
    for entry in requested:
        if isinstance(entry, bool):  # bool is an int subclass; never a variant
            raise TypeError(
                f"task {task_path}: variant {entry!r} is a bool; use an index "
                f"(int) or a variant name (str)"
            )
        if isinstance(entry, int):
            if n and not (0 <= entry < n):
                raise ValueError(
                    f"task {task_path}: variant index {entry} out of range; "
                    f"task declares {n} variant(s): {declared}"
                )
            out.append((entry, declared[entry] if n else ""))
        elif isinstance(entry, str):
            if entry not in declared:
                raise ValueError(
                    f"task {task_path}: no variant named {entry!r}; declared "
                    f"variants (in order) are {declared}"
                )
            out.append((declared.index(entry), entry))
        else:  # unreachable via config_loader, but keep enumerate honest
            raise TypeError(
                f"task {task_path}: variant {entry!r} must be an int index or "
                f"a str name"
            )
    return out


class Runner:
    """Owns the provider router; produces and executes run units."""

    def __init__(self, spec: ExperimentSpec):
        if spec.roles is None or spec.a2a is None:
            raise ValueError(
                "only a2a experiments are supported — the experiment "
                "yaml's `agents:` must be a role→preset mapping"
            )
        self._spec = spec
        # Router resolves each unit's snapshot to its provider, building +
        # caching provider instances lazily (keeps --dry-run provider-free).
        self._router = EnvironmentRouter(spec.environment)
        self._output_root = Path(spec.output.root) / spec.name

    @property
    def spec(self) -> ExperimentSpec:
        return self._spec

    @property
    def output_root(self) -> Path:
        return self._output_root

    # ---- enumeration ----

    def enumerate_units(self) -> list[PairRunUnit]:
        """One :class:`PairRunUnit` per task × variant — the fixed
        role→agent binding replaces the agent axis.

        ``agents:`` may bind a SUPERSET of a task's roles: a corpus whose
        variants seat different specialist sets (a two-agent matter here, a
        five-agent matter there) is run from one yaml that names every role
        that can appear, and each unit binds only the roles its variant
        declares. A variant declaring a role the yaml does not bind is skipped
        with a log line rather than enumerated into a failing unit. If every
        listed variant is skipped, that's a misconfiguration — raise with the
        role sets seen.
        """
        from ..tasks.loader import TaskLoader

        bound = set(self._spec.roles)
        out: list[PairRunUnit] = []
        skipped: list[tuple[str, list[str]]] = []
        for task in self._spec.tasks:
            loader = TaskLoader(str(Path("tasks") / task.path))
            try:
                declared = loader.variant_names()
            except Exception as exc:
                raise RuntimeError(
                    f"cannot load variants of task {task.path}: {exc}"
                ) from exc
            # Resolve requested variants (names or indices, or None ⇒ all) to
            # concrete (index, name) pairs; slug uses the name, everything
            # downstream (staging/eval) uses the index.
            for vi, variant_name in _select_variants(
                task.variants, declared, task.path
            ):
                try:
                    task_roles = list(loader.load(vi).get("roles") or {})
                except Exception as exc:
                    raise RuntimeError(
                        f"cannot enumerate task {task.path} variant {vi}: {exc}"
                    ) from exc
                if not task_roles or not set(task_roles) <= bound:
                    label = f"{task.path}[{variant_name or vi}]"
                    skipped.append((label, sorted(task_roles)))
                    logger.info(
                        "enumerate_units: skipping %s (declares roles %s; "
                        "this experiment binds %s)",
                        label, sorted(task_roles), sorted(bound),
                    )
                    continue
                out.append(PairRunUnit(
                    # Bind in the task's role order, only the roles it declares.
                    roles={r: self._spec.roles[r] for r in task_roles},
                    task_path=task.path,
                    variant_index=vi,
                    variant_name=variant_name,
                ))
        if skipped and not out:
            raise RuntimeError(
                f"no task in the tasks file matches the experiment's roles "
                f"{sorted(bound)} — tasks seen: "
                + ", ".join(f"{p} (roles {r})" for p, r in skipped)
            )
        return out

    # ---- execution ----

    async def run(
        self,
        units: Iterable[PairRunUnit] | None = None,
    ) -> list[UnitResult]:
        """Run all units (or a filtered subset). Returns ``list[UnitResult]``.

        No aggregation, no summary — caller does whatever rollup it wants.
        """
        from .pair_lifecycle import run_pair_unit
        from .unit_common import install_signal_handlers

        install_signal_handlers()
        unit_list = list(units) if units is not None else self.enumerate_units()
        if not unit_list:
            logger.warning("Runner.run: no units to execute")
            return []

        self._output_root.mkdir(parents=True, exist_ok=True)

        n = self._spec.concurrency
        sem = asyncio.Semaphore(n)
        logger.info("runner: %d units, concurrency=%d", len(unit_list), n)

        async def _drive(u: PairRunUnit) -> UnitResult:
            logger.info("[%s] queued", u.slug)
            # One unit holds ONE semaphore slot but boots one container
            # per role — size `concurrency` accordingly.
            result = await run_pair_unit(
                unit=u,
                router=self._router,
                output_root=self._output_root,
                a2a=self._spec.a2a,
                artifacts=self._spec.artifacts,
                sem=sem,
                cleanup_mode=self._spec.cleanup_mode,
                wall_time_s=self._spec.wall_time_s,
            )
            logger.info("[%s] done: status=%s score=%s duration=%.1fs",
                        u.slug, result.status, result.score, result.duration_s or 0)
            return result

        results = await asyncio.gather(
            *(_drive(u) for u in unit_list),
            return_exceptions=False,
        )
        return list(results)
