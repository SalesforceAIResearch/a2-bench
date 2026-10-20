"""Run one interest-seeking negotiation experiment and print its API cost estimate.

This is a thin scenario-owned wrapper around the unchanged ``a2_run`` CLI.  It
passes every argument through, detects run artifacts created by this invocation,
runs the task's deterministic analyzer on each one, and prints a cost block even
when the experiment itself failed or timed out.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from a2_run.orchestration.config_loader import load_experiment
from a2_run.orchestration.runner import Runner

TASK_PATH = "procurement/interest_seeking_negotiation"
ANALYZER_MODULE = "tasks.procurement.interest_seeking_negotiation.analyze"


def _overrides(args: list[str]) -> list[str]:
    """Extract ``-S``/``--set`` values while leaving all args for passthrough."""
    values: list[str] = []
    index = 0
    while index < len(args):
        arg = args[index]
        if arg in {"-S", "--set"}:
            if index + 1 >= len(args):
                raise ValueError(f"{arg} requires KEY=VALUE")
            values.append(args[index + 1])
            index += 2
            continue
        if arg.startswith("--set="):
            values.append(arg.split("=", 1)[1])
        elif arg.startswith("-S") and arg != "-S":
            values.append(arg[2:])
        index += 1
    return values


def _run_dirs(root: Path) -> set[Path]:
    if not root.is_dir():
        return set()
    return {path.parent.resolve() for path in root.rglob("run.json")}


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0].startswith("-"):
        print(
            "usage: python -m tasks.procurement.interest_seeking_negotiation."
            "run_and_report <experiment.yaml> [a2_run run options]",
            file=sys.stderr,
        )
        return 2

    spec_path = Path(args[0])
    runner_args = args[1:]
    try:
        spec = load_experiment(spec_path, overrides=_overrides(runner_args))
        runner = Runner(spec)
        units = runner.enumerate_units()
    except (KeyError, OSError, RuntimeError, TypeError, ValueError) as exc:
        print(f"error: cannot resolve experiment: {exc}", file=sys.stderr)
        return 2

    wrong_tasks = sorted({unit.task_path for unit in units if unit.task_path != TASK_PATH})
    if wrong_tasks:
        print(
            "error: this cost-reporting wrapper is restricted to "
            "procurement/interest_seeking_negotiation; got "
            + ", ".join(wrong_tasks),
            file=sys.stderr,
        )
        return 2

    output_root = runner.output_root.resolve()
    before = _run_dirs(output_root)
    command = [sys.executable, "-m", "a2_run", "run", str(spec_path), *runner_args]
    print(f"cost-reporting output root: {output_root}", flush=True)

    try:
        completed = subprocess.run(command, check=False)
        run_code = completed.returncode
    except KeyboardInterrupt:
        run_code = 130

    if "--dry-run" in runner_args:
        print("dry-run: no model inference occurred; no API cost artifact was created.")
        return run_code

    new_runs = sorted(
        _run_dirs(output_root) - before,
        key=lambda path: path.stat().st_mtime,
    )
    if not new_runs:
        print(
            "API cost summary unavailable: the invocation created no run.json artifact. "
            "Check the runner error above; no zero-dollar claim is being made.",
            file=sys.stderr,
        )
        return run_code or 1

    analysis_failed = False
    for run_dir in new_runs:
        analysis = subprocess.run(
            [sys.executable, "-m", ANALYZER_MODULE, str(run_dir)],
            check=False,
        )
        analysis_failed = analysis_failed or analysis.returncode != 0

    if run_code != 0:
        return run_code
    return 1 if analysis_failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
