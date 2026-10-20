"""CLI entry: ``python -m a2_run run example_exp_a2a.yaml``."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

from .orchestration import Runner
from .orchestration.config_loader import load_experiment
from .orchestration.experiment_spec import PairRunUnit
from .orchestration.run_writer import slug_agent, slug_model, slug_task, slug_variant
from .transcript.server import serve

logger = logging.getLogger(__name__)

# A unit is considered "already done" (resume-skippable) when a prior run of it
# reached a terminal state we don't want to repeat: either it finished cleanly
# (``completed``) or the agent used its full wall-clock budget (``timeout``).
# Other states (failed / cancelled / not_executed) are re-run.
_RESUME_DONE_STATUSES = frozenset({"completed", "timeout"})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="a2",
        description="a2-bench: run agent-to-agent benchmark experiments.",
    )
    subparsers = parser.add_subparsers(dest="cmd", required=True)

    p_run = subparsers.add_parser("run", help="Run an experiment yaml.")
    p_run.add_argument("spec_path", type=Path, help="Path to experiment yaml.")
    p_run.add_argument("--dry-run", action="store_true",
                       help="Show the run matrix without executing.")
    p_run.add_argument(
        "--set", "-S", action="append", dest="overrides", metavar="KEY=VALUE",
        help="Override any experiment key before validation (repeatable). "
             "KEY is a dotted path into the yaml; VALUE is parsed as yaml. "
             "Examples: -S agents.shopper=configs/agents/codex.yaml "
             "-S tasks=selected_tasks/a2a_hello.txt -S a2a.max_messages=20 "
             "-S output.root=.logs/sweep1. Relative paths resolve against "
             "the yaml's directory, as if written in the file.",
    )
    p_run.add_argument("--agent", action="append", dest="filter_agents",
                       metavar="ID", help="Filter: only run agents with these ids.")
    p_run.add_argument("--task", action="append", dest="filter_tasks",
                       metavar="PATH", help="Filter: only run these task paths.")
    p_run.add_argument(
        "--resume", action="store_true",
        help="Skip any (agent, task, variant) unit that already has a prior run "
             "whose status is 'completed' or 'timeout' under the output dir; "
             "re-run everything else. Lets a re-invocation fill only the gaps.",
    )
    p_run.add_argument("--verbose", "-v", action="store_true")

    p_serve = subparsers.add_parser(
        "serve", help="Serve a live, on-demand viewer for finished run(s).")
    p_serve.add_argument(
        "path", type=Path,
        help="A single run dir, or a tree of runs (e.g. .logs/<experiment>).")
    p_serve.add_argument("--host", default="127.0.0.1", help="Bind host.")
    p_serve.add_argument("--port", type=int, default=8000, help="Bind port.")
    p_serve.add_argument("--verbose", "-v", action="store_true")

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if getattr(args, "verbose", False) else logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
    )

    if args.cmd == "run":
        return asyncio.run(_cmd_run(args))
    if args.cmd == "serve":
        return _cmd_serve(args)
    return 1


# =============================================================================
# Commands
# =============================================================================

async def _cmd_run(args: argparse.Namespace) -> int:
    spec = load_experiment(args.spec_path, overrides=args.overrides)
    runner = Runner(spec)
    units = _filter_units(runner.enumerate_units(), args)

    if args.resume:
        units = _filter_resume(units, runner.output_root)

    if args.dry_run:
        env = spec.environment
        kinds = sorted(set(env.snapshot_kind.values()) | ({env.default_kind} if env.default_kind else set()))
        if env.snapshot_kind:
            routing = ", ".join(f"{s}->{k}" for s, k in sorted(env.snapshot_kind.items()))
            env_desc = f"{'+'.join(kinds)} ({routing})"
        else:
            env_desc = "+".join(kinds)
        print(f"experiment: {spec.name}")
        role_desc = ", ".join(f"{r}={s.class_}" for r, s in spec.roles.items())
        print(f"mode:       a2a ({role_desc}; max_messages={spec.a2a.max_messages}, "
              f"channel={spec.a2a.channel})")
        print(f"environment: {env_desc}")
        print(f"output:     {runner.output_root}")
        print(f"concurrency: {spec.concurrency}")
        print(f"units ({len(units)}):")
        for u in units:
            var = slug_variant(u.variant_name, u.variant_index)
            print(f"  {u.agent_id:20s}  {u.task_path:40s}  {var}")
        return 0

    results = await runner.run(units)
    _print_results_table(results)
    bad = sum(1 for r in results if r.status not in ("completed",))
    return 0 if bad == 0 else 1


def _cmd_serve(args: argparse.Namespace) -> int:
    path: Path = args.path
    if not path.exists():
        print(f"error: no such path: {path}", file=sys.stderr)
        return 1
    serve(path, host=args.host, port=args.port)
    return 0


# =============================================================================
# Helpers
# =============================================================================

def _filter_units(units: list[PairRunUnit], args: argparse.Namespace) -> list[PairRunUnit]:
    if args.filter_agents:
        units = [u for u in units if u.agent_id in args.filter_agents]
    if args.filter_tasks:
        units = [u for u in units if u.task_path in args.filter_tasks]
    return units


def _unit_already_done(unit: PairRunUnit, output_root: Path) -> bool:
    """True if a prior run of this unit reached a resume-skippable status.

    Mirrors :class:`RunWriter`'s on-disk layout
    ``<output_root>/<agent>/<model>/<task>/<variant>/<ts>/run.json`` and scans
    every timestamped run dir; the unit is "done" if ANY of them has a
    ``status`` in :data:`_RESUME_DONE_STATUSES`. Unreadable / malformed run.json
    files are ignored (treated as not-done, so the unit re-runs).
    """
    model = unit.model_id
    v_dir = (
        output_root
        / slug_agent(unit.agent_id)
        / slug_model(model)
        / slug_task(unit.task_path)
        / slug_variant(unit.variant_name, unit.variant_index)
    )
    if not v_dir.is_dir():
        return False
    for ts_dir in v_dir.iterdir():
        run_json = ts_dir / "run.json"
        if not run_json.is_file():
            continue
        try:
            status = json.loads(run_json.read_text(encoding="utf-8")).get("status")
        except (OSError, ValueError):
            continue
        if status in _RESUME_DONE_STATUSES:
            return True
    return False


def _filter_resume(units: list[PairRunUnit], output_root: Path) -> list[PairRunUnit]:
    """Drop units that already have a completed/timeout run on disk."""
    keep = [u for u in units if not _unit_already_done(u, output_root)]
    skipped = len(units) - len(keep)
    if skipped:
        logger.info(
            "resume: %d unit(s) already completed/timeout — skipping; running %d",
            skipped, len(keep),
        )
    else:
        logger.info("resume: no prior completed/timeout runs found; running all %d", len(keep))
    return keep


def _print_results_table(results) -> None:
    if not results:
        print("(no results)")
        return
    print()
    print(f"{'agent':20s}  {'task':40s}  {'variant':12s}  {'status':10s}  {'score':>6s}  {'dur':>6s}")
    print("-" * 108)
    for r in results:
        score = f"{r.score:.2f}" if r.score is not None else "  -  "
        dur = f"{r.duration_s:.1f}s" if r.duration_s is not None else "   -  "
        var = slug_variant(r.unit.variant_name, r.unit.variant_index)
        print(
            f"{r.unit.agent_id:20s}  {r.unit.task_path:40s}  "
            f"{var:12s}  {r.status:10s}  {score:>6s}  {dur:>6s}"
        )


if __name__ == "__main__":
    sys.exit(main())
