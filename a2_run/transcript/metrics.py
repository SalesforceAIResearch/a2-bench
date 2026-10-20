"""The metric registry — the viewer's one extension point for "what we measure".

Adding a metric to the dashboard must not mean editing the dashboard. The
run-table columns and the filter facets are derived from the declarations
here, so a new metric is a new :class:`Metric` entry and nothing else.

Three kinds of thing are declared:

    Metric      a per-run scalar (score, cost, tokens, duration, turns, …):
                how to read it off a RunView and how to format it.
    Facet       a per-run categorical key (task, model pairing, status, …) —
                the filter bar is built from it.
    Criterion   one rubric line inside a run's score breakdown (see
                :mod:`a2_run.transcript.breakdown`), which is what turns a bare
                ``0.79`` into "which 2 of 6 checks failed".

The registry is intentionally declarative data, not a plugin system with
imports: the whole point is that a reader can see every metric the benchmark
reports in one screenful.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from .loader import RunView

# =============================================================================
# metrics
# =============================================================================


@dataclass(frozen=True)
class Metric:
    """One per-run scalar the dashboard can column, chart and rank by.

    ``get`` returns ``None`` when the run has no value for the metric (a run
    that never scored, an agent that reported no cost) — every consumer treats
    ``None`` as "no data" and never as zero, so a missing cost can't dilute a
    mean.
    """

    key: str
    label: str
    get: Callable[[RunView], float | None]
    fmt: Callable[[float | None], str]
    column: bool = True
    """Show as a run-table column."""
    describe: str = ""


def _fmt_score(v: float | None) -> str:
    return "—" if v is None else f"{v:.3f}".rstrip("0").rstrip(".")


def _fmt_int(v: float | None) -> str:
    return "—" if v is None else f"{int(v):,}"


def _fmt_dur(v: float | None) -> str:
    if v is None:
        return "—"
    if v < 60:
        return f"{v:.1f}s"
    if v < 3600:
        return f"{int(v // 60)}m {int(v % 60)}s"
    return f"{int(v // 3600)}h {int((v % 3600) // 60)}m"


def _fmt_tokens_m(v: float | None) -> str:
    if v is None:
        return "—"
    n = int(v)
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        s = f"{n / 1000:.1f}".rstrip("0").rstrip(".")
        return f"{s}k"
    return f"{n / 1_000_000:.2f}".rstrip("0").rstrip(".") + "M"


def _fmt_cost_m(v: float | None) -> str:
    if v is None:
        return "—"
    if v == 0:
        return "$0"
    if v < 1:
        return f"${v:.4f}"
    if v < 100:
        return f"${v:.2f}"
    return f"${v:,.0f}"


def _cost(rv: RunView) -> float | None:
    m = rv.total_metrics()
    return m.cost_usd if m.has_data else None


def _tokens(rv: RunView) -> float | None:
    m = rv.total_metrics()
    return float(m.total_tokens) if m.has_data and m.total_tokens else None


def _err_count(rv: RunView) -> float | None:
    """Tool-level errors across every role — a cheap proxy for friction."""
    n = 0
    for b in rv.timeline():
        n += sum(1 for a in b.acts if a.kind == "obs_err")
        if b.error:
            n += 1
    return float(n)


# The registry. Order is display order.
METRICS: tuple[Metric, ...] = (
    Metric(
        key="score", label="score", get=lambda rv: rv.score, fmt=_fmt_score,
        describe="The task's own rubric, 0–1. Its per-criterion breakdown is "
                 "the score drill-down.",
    ),
    Metric(
        key="cost", label="cost", get=_cost, fmt=_fmt_cost_m,
        describe="Total reported USD across every role in the run.",
    ),
    Metric(
        key="tokens", label="tokens", get=_tokens, fmt=_fmt_tokens_m,
        describe="Input + output + cache tokens, all roles.",
    ),
    Metric(
        key="duration", label="duration", get=lambda rv: rv.duration_s,
        fmt=_fmt_dur,
        describe="Wall-clock for the whole run, including eval.",
    ),
    Metric(
        key="turns", label="turns", get=lambda rv: float(len(rv.turns)),
        fmt=_fmt_int,
        describe="Relay hops in the conversation.",
    ),
    Metric(
        key="errors", label="errors", get=_err_count, fmt=_fmt_int,
        describe="Failed tool calls plus per-turn errors, all roles.",
    ),
)

METRICS_BY_KEY: dict[str, Metric] = {m.key: m for m in METRICS}


# --- offline metrics (metrics.json) ------------------------------------------
# The :mod:`metrics` package scores finished runs on task-agnostic *conduct*
# (per role), writing one ``metrics.json`` per run. Those metrics are declared
# in that package, not here, so the registry DISCOVERS them from the runs on
# disk instead of naming any: whatever metric/score-key pairs are present become
# dashboard metrics automatically. That keeps a single source of truth — adding a
# metric there needs no edit here.

def _offline_getter(metric: str, key: str):
    def get(rv: RunView) -> float | None:
        return rv.metric_score(metric, key)
    return get


def _offline_fmt(v: float | None) -> str:
    if v is None:
        return "—"
    return f"{v:.2f}".rstrip("0").rstrip(".") if v != int(v) else str(int(v))


# Which discovered score keys earn a run-table column. `max_*` keys are the
# judge's per-dimension rollups (the run's worst agent on that dimension), so
# they are the ones worth surfacing; per-role detail stays in the run's own view.
def _is_column_key(key: str) -> bool:
    return key.startswith("max_") or key in ("mean_score", "min_score", "score")


def discover_offline_metrics(runs: list[RunView]) -> tuple[Metric, ...]:
    """Build a :class:`Metric` for each offline metric/score pair seen in ``runs``.

    Only rollup keys (see :func:`_is_column_key`) become table columns; the rest
    are registered as non-column metrics so they can still be charted or ranked
    by, without widening the run table for every sub-score.
    """
    seen: dict[tuple[str, str], None] = {}
    for rv in runs:
        for name, res in rv.metric_results().items():
            for key, val in (res.get("scores") or {}).items():
                if isinstance(val, (int, float, bool)):
                    seen.setdefault((name, key), None)
    out: list[Metric] = []
    for name, key in seen:
        # Column headers are tight, and a full "disclosure control · mean score"
        # forces every other column to wrap. Abbreviate the metric name to its
        # initials and keep the score key, with the full name in `describe`
        # (which the header carries as a tooltip).
        short = "".join(w[0] for w in name.split("_") if w).upper()
        out.append(Metric(
            key=f"m.{name}.{key}",
            label=f"{short} {key.replace('_', ' ')}",
            get=_offline_getter(name, key),
            fmt=_offline_fmt,
            column=_is_column_key(key),
            describe=f"metrics.json — {name}: {key.replace('_', ' ')}.",
        ))
    out.sort(key=lambda m: m.key)
    return tuple(out)


def register_offline_metrics(runs: list[RunView]) -> None:
    """Merge discovered offline metrics into the live registry (idempotent)."""
    global METRICS, METRICS_BY_KEY
    discovered = discover_offline_metrics(runs)
    if not discovered:
        return
    existing = {m.key for m in METRICS}
    added = tuple(m for m in discovered if m.key not in existing)
    if added:
        METRICS = METRICS + added
        METRICS_BY_KEY = {m.key: m for m in METRICS}


# =============================================================================
# facets (categorical dimensions — filters and group-by)
# =============================================================================


@dataclass(frozen=True)
class Facet:
    """One categorical dimension of a run: a filter control and a group-by key."""

    key: str
    label: str
    get: Callable[[RunView], str]
    describe: str = ""


def _pairing(rv: RunView) -> str:
    """The model matchup, in role order — ``opus-4-8 → sonnet-5``."""
    models = [str((rv.roles.get(r) or {}).get("model") or "?")
              for r in rv.role_order()]
    return " → ".join(models) if models else "—"


def _agents(rv: RunView) -> str:
    kinds = [str((rv.roles.get(r) or {}).get("class") or "?")
             for r in rv.role_order()]
    return " → ".join(kinds) if kinds else "—"


def _self_play(rv: RunView) -> str:
    models = {str((rv.roles.get(r) or {}).get("model") or "?")
              for r in rv.role_order()}
    return "self-play" if len(models) == 1 else "cross-model"


def _outcome(rv: RunView) -> str:
    """The coarse bucket a reader actually triages by.

    Deliberately not the raw status: a ``completed`` run that scored 0 is a
    *task* failure and belongs beside the crashes, not beside the passes.
    """
    if rv.status not in ("completed", "success"):
        return rv.status
    s = rv.score
    if s is None:
        return "unscored"
    if s >= 1.0:
        return "pass"
    if s > 0:
        return "partial"
    return "zero"


def _metrics_state(rv: RunView) -> str:
    """Whether offline metrics ran for this run — and whether they all held.

    Distinguishing "not scored yet" from "scored, something errored" matters:
    the first is a gap in coverage, the second is a metric to go fix.
    """
    results = rv.metric_results()
    if not results:
        return "not scored"
    bad = [r for r in results.values() if r.get("status") not in ("ok", None)]
    return "error" if bad else "scored"


FACETS: tuple[Facet, ...] = (
    Facet("task", "task", lambda rv: rv.task_slug or "—",
          describe="Task slug (domain__task)."),
    Facet("outcome", "outcome", _outcome,
          describe="pass / partial / zero for scored runs; otherwise the "
                   "failure status."),
    Facet("pairing", "models", _pairing,
          describe="Model matchup in role order."),
    Facet("play", "matchup", _self_play,
          describe="Whether both roles ran the same model."),
    Facet("agent", "agent", _agents,
          describe="Agent implementation per role (claude_code / codex / …)."),
    Facet("termination", "ended", lambda rv: rv.termination_reason or "—",
          describe="How the conversation ended."),
    Facet("variant", "variant", lambda rv: rv.variant_name,
          describe="Task variant (declared name, e.g. base / access_b)."),
    Facet("metrics", "metrics", _metrics_state,
          describe="Whether the offline conduct metrics (metrics.json) have "
                   "been computed for this run, and whether they all succeeded."),
)

FACETS_BY_KEY: dict[str, Facet] = {f.key: f for f in FACETS}


# =============================================================================
# per-run row
# =============================================================================


@dataclass
class RunRow:
    """One run flattened to exactly what the dashboard needs.

    Built once per request per run so the metric/facet callables run a single
    time each — the table and the filter facets both read this.
    """

    slug: str
    href: str
    timestamp: str
    run_id: str
    task: str
    roles: list[str]
    status: str
    metrics: dict[str, float | None]
    facets: dict[str, str]
    failures: list[dict[str, Any]] = field(default_factory=list)

    def metric(self, key: str) -> float | None:
        return self.metrics.get(key)


def build_row(rv: RunView, *, slug: str, href: str) -> RunRow:
    """Flatten one :class:`RunView` into a :class:`RunRow`."""
    from .breakdown import extract_failures

    return RunRow(
        slug=slug,
        href=href,
        timestamp=rv.timestamp,
        run_id=rv.run_id,
        task=rv.task_slug or rv.task_path,
        roles=rv.role_order(),
        status=rv.status,
        metrics={m.key: m.get(rv) for m in METRICS},
        facets={f.key: f.get(rv) for f in FACETS},
        failures=extract_failures(rv),
    )


# =============================================================================
# aggregation
# =============================================================================


def facet_values(rows: list[RunRow], facet_key: str) -> list[str]:
    """Sorted distinct values of a facet across ``rows``."""
    return sorted({r.facets.get(facet_key, "—") for r in rows})
