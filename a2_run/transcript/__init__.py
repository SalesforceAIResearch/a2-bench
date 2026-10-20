"""Transcript viewer: browse finished runs, filter them, and dig into results.

Offline, read-only, on demand. Five layers:

    loader      one run dir → a tolerant :class:`RunView` (:func:`load_run`)
    metrics     the registry of per-run metrics + filter facets — the one place
                to declare a new thing the dashboard measures
    breakdown   why a run scored what it did (:func:`extract_breakdown`) and
                everything that went wrong in it (:func:`extract_failures`)
    dashboard   the filterable, tabbed view over many runs
    viewer      one run as a merged timeline (what each agent did, then said)

:func:`serve` runs a local HTTP server that renders all of it on demand. Nothing
is written to disk and nothing touches the network beyond the localhost server.
"""

from .breakdown import extract_breakdown, extract_failures
from .dashboard import facet_universe, render_dashboard
from .loader import (
    Act,
    ImageRef,
    Metrics,
    RunView,
    StepView,
    Turn,
    TurnBlock,
    iter_runs,
    load_run,
)
from .metrics import FACETS, METRICS, Facet, Metric, RunRow, build_row
from .server import serve
from .viewer import render_run

__all__ = [
    "Act",
    "FACETS",
    "Facet",
    "ImageRef",
    "METRICS",
    "Metric",
    "Metrics",
    "RunRow",
    "RunView",
    "StepView",
    "Turn",
    "TurnBlock",
    "build_row",
    "extract_breakdown",
    "extract_failures",
    "facet_universe",
    "iter_runs",
    "load_run",
    "render_dashboard",
    "render_run",
    "serve",
]
