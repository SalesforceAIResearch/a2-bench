"""CLI: ``python -m metrics score <path>`` / ``python -m metrics list``.

Separate from ``a2_run`` because metrics are offline: you score archived runs —
one run dir or a whole ``.logs`` tree — without provisioning anything.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

from a2_run.orchestration.config_loader import _load_dotenv
from a2_run.transcript.loader import RunView, iter_runs

from .base import (
    MetricReport,
    available_metrics,
    get_metric,
    metric_class,
    run_metrics,
    write_metrics,
)

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[1]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="metrics", description="Compute a2a interaction metrics over finished runs.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="List registered metrics.")

    p = sub.add_parser("score", help="Score a run dir or a tree of runs.")
    p.add_argument("path", type=Path, help="A run dir, or a tree (e.g. .logs/<exp>).")
    p.add_argument("--metric", action="append", dest="metrics", metavar="NAME",
                   help="Metric to compute (repeatable). Default: all.")
    p.add_argument("--model", default=None, help="Judge model. Default: claude-opus-5.")
    p.add_argument("--provider", default=None,
                   choices=["direct", "vertex", "bedrock"],
                   help="Judge routing. Default: direct.")
    p.add_argument("--secret-file", type=Path, default=None,
                   help="dotenv with judge credentials. Default: secret/.env.")
    p.add_argument("--concurrency", type=int, default=2,
                   help="Runs scored in parallel (default 2).")
    p.add_argument("--dry-run", action="store_true", help="List runs; compute nothing.")
    p.add_argument("--overwrite", action="store_true",
                   help="Replace metrics.json instead of merging into it.")
    p.add_argument("--stdout", action="store_true",
                   help="Print the report instead of writing metrics.json.")
    p.add_argument("--verbose", "-v", action="store_true")

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if getattr(args, "verbose", False) else logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
    )

    if args.cmd == "list":
        for name in available_metrics() or []:
            cls = metric_class(name)
            print(f"{name:22s}  L{cls.level}  {cls.description}")
        return 0
    return asyncio.run(_score(args))


async def _score(args: argparse.Namespace) -> int:
    if not args.path.exists():
        print(f"error: no such path: {args.path}", file=sys.stderr)
        return 1

    runs = list(iter_runs(args.path))
    if not runs:
        print(f"error: no run directories under {args.path}", file=sys.stderr)
        return 1

    names = args.metrics or available_metrics()
    unknown = [n for n in names if n not in available_metrics()]
    if unknown:
        print(f"error: unknown metric(s): {', '.join(unknown)}", file=sys.stderr)
        return 1

    if args.dry_run:
        print(f"metrics:  {', '.join(names)}")
        for run in runs:
            print(f"  {run.run_dir}")
        return 0

    # Judge credentials come from the same secret/.env an agent run uses.
    if args.secret_file:
        _load_dotenv(args.secret_file, override=True)
    else:
        _load_dotenv(_REPO_ROOT / "secret" / ".env")
        _load_dotenv(_REPO_ROOT / ".env")

    kwargs = {k: v for k, v in (("model", args.model), ("provider", args.provider))
              if v is not None}
    sem = asyncio.Semaphore(max(1, args.concurrency))

    async def one(run: RunView) -> MetricReport:
        async with sem:
            report = await run_metrics(run, [get_metric(n, **kwargs) for n in names])
        if not args.stdout:
            logger.info("wrote %s",
                        write_metrics(report, run.run_dir, merge=not args.overwrite))
        return report

    reports = await asyncio.gather(*(one(r) for r in runs))

    if args.stdout:
        print(json.dumps([r.model_dump(mode="json") for r in reports],
                         indent=2, ensure_ascii=False))
    else:
        _print_table(reports)

    return 0 if all(r.ok for r in reports) else 1


def _print_table(reports: list[MetricReport]) -> None:
    print()
    print(f"{'run':44s}  {'metric':20s}  {'status':8s}  scores")
    print("-" * 110)
    for report in reports:
        label = report.run_dir[-44:]
        for name, res in sorted(report.results.items()):
            scores = ", ".join(f"{k}={v}" for k, v in res.scores.items()
                               if v is not None)
            print(f"{label:44s}  {name:20s}  {res.status:8s}  "
                  f"{scores or res.error or res.summary or '-'}")
            label = ""


if __name__ == "__main__":
    sys.exit(main())
