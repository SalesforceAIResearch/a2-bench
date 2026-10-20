"""Turn a run's outcome into *why* — the score rubric and the failure list.

Two extractions, both tolerant of runs written before the producers existed:

``extract_breakdown``
    Reads the per-role subscores and per-criterion pass/fail that a task's
    ``evaluate()`` reports under ``eval_result.json``'s ``breakdown`` key (see
    :func:`a2_run.orchestration.run_writer.RunWriter.write_eval_result`). Old
    runs have no ``breakdown``, so this returns ``None`` and the viewer shows
    the bare score — the drill-down degrades, it never errors.

``extract_failures``
    Normalizes everything that went wrong in a run into one flat list of
    typed records, whatever layer it happened at: the run itself failing or
    timing out, a conversation ending abnormally, a turn erroring, a scored
    criterion coming back false, or an individual tool call returning an error.
    The failures tab is a filter over this list, so a new failure source means
    appending one more probe here rather than touching the renderer.
"""

from __future__ import annotations

from typing import Any

from .loader import RunView


# =============================================================================
# score breakdown
# =============================================================================


def _norm_criteria(raw: Any) -> list[dict[str, Any]]:
    """``{name: bool|float}`` → ordered ``[{name, passed, weight}]``."""
    if not isinstance(raw, dict):
        return []
    out: list[dict[str, Any]] = []
    for name, val in raw.items():
        if isinstance(val, bool):
            passed, frac = val, (1.0 if val else 0.0)
        elif isinstance(val, (int, float)):
            frac = max(0.0, min(1.0, float(val)))
            passed = frac >= 1.0
        else:
            # A non-numeric criterion value (a reason string, a nested dict) is
            # information, not a gate — surface it without claiming pass/fail.
            out.append({"name": str(name), "passed": None,
                        "fraction": None, "note": str(val)[:300]})
            continue
        out.append({"name": str(name), "passed": passed, "fraction": frac})
    return out


def extract_breakdown(rv: RunView) -> dict[str, Any] | None:
    """The run's score rubric, or ``None`` when the task reported none.

    Shape::

        {"total": 0.79,
         "parts": [{"role": "trainer", "score": 0.88, "weight": 0.5,
                    "criteria": [{"name": "keys_match", "passed": True}, …],
                    "reason": None, "facts": {...}}, …]}

    ``parts`` are usually per-role (each side of an a2a pair scores its own
    deliverables) but nothing requires that — a task may report any named
    components it likes.
    """
    raw = rv.eval.get("breakdown")
    if not isinstance(raw, dict):
        return None

    parts_raw = raw.get("parts")
    if not isinstance(parts_raw, list):
        return None

    parts: list[dict[str, Any]] = []
    for p in parts_raw:
        if not isinstance(p, dict):
            continue
        score = p.get("score")
        parts.append({
            "role": str(p.get("role") or p.get("name") or "—"),
            "score": float(score) if isinstance(score, (int, float)) else None,
            "weight": (float(p["weight"])
                       if isinstance(p.get("weight"), (int, float)) else None),
            "criteria": _norm_criteria(p.get("criteria")),
            # Why a whole component zeroed (a missing deliverable), when the
            # task reports it instead of per-criterion detail.
            "reason": (str(p["reason"]) if p.get("reason") else None),
            # Free-form measured values the task chose to surface (counts,
            # verdicts, tolerances) — shown as a key/value list, never scored.
            "facts": p.get("facts") if isinstance(p.get("facts"), dict) else {},
        })
    if not parts:
        return None

    total = raw.get("total")
    return {
        "total": float(total) if isinstance(total, (int, float)) else rv.score,
        "parts": parts,
        "note": str(raw["note"]) if raw.get("note") else None,
    }


# =============================================================================
# failures
# =============================================================================

# Failure severity, used for ordering and for the status color. "blocked" means
# the run never produced a usable measurement; "soft" is a run that completed
# but lost points.
_SEV_ORDER = {"blocked": 0, "hard": 1, "soft": 2}


def _fail(kind: str, severity: str, summary: str, *, where: str = "",
          detail: str = "", turn: int | None = None) -> dict[str, Any]:
    return {
        "kind": kind, "severity": severity, "summary": summary,
        "where": where, "detail": detail, "turn": turn,
    }


def extract_failures(rv: RunView) -> list[dict[str, Any]]:
    """Every distinct thing that went wrong in this run, most severe first.

    Sources, in order of how much they invalidate the run:

    * ``run`` — the run itself failed / timed out / was cancelled.
    * ``eval`` — ``evaluate()`` errored or timed out, so there is no score.
    * ``termination`` — the conversation ended abnormally
      (``invalid_agent_response``, ``wall_time``, ``agent_failed``) rather than
      by ``task_complete``.
    * ``criterion`` — a scored rubric check came back false.
    * ``turn`` — a single turn carried an error or a non-completed status.
    * ``tool`` — an individual tool call returned an error observation.
    """
    out: list[dict[str, Any]] = []

    # ---- run level ----
    if rv.status not in ("completed", "success"):
        err = (rv.run.get("termination") or {}).get("error")
        msg = ""
        if isinstance(err, dict):
            msg = str(err.get("message") or "")
        elif err:
            msg = str(err)
        out.append(_fail(
            "run", "blocked", f"run {rv.status}",
            where="run", detail=msg,
        ))

    # ---- eval level ----
    eval_status = rv.eval.get("eval_status")
    if eval_status and eval_status not in ("success",):
        ee = rv.eval.get("error")
        detail = ""
        if isinstance(ee, dict):
            detail = str(ee.get("message") or "")
        elif ee:
            detail = str(ee)
        out.append(_fail(
            "eval", "blocked", f"eval {eval_status}",
            where="evaluate()", detail=detail,
        ))

    # ---- conversation termination ----
    reason = rv.termination_reason
    if reason and reason not in ("task_complete",):
        # ``empty_reply`` is retired but still appears in archived runs, which
        # this reads — keep classifying it.
        sev = ("hard" if reason in ("empty_reply", "agent_failed",
                                    "invalid_agent_response") else "soft")
        out.append(_fail(
            "termination", sev, f"ended {reason}",
            where="conversation", detail=rv.end_reason or "",
        ))

    # ---- scored criteria ----
    bd = extract_breakdown(rv)
    if bd:
        for part in bd["parts"]:
            if part.get("reason"):
                out.append(_fail(
                    "criterion", "hard", part["reason"],
                    where=part["role"],
                ))
            for c in part["criteria"]:
                if c.get("passed") is False:
                    out.append(_fail(
                        "criterion", "soft", c["name"], where=part["role"],
                    ))

    # ---- turn + tool level ----
    for i, b in enumerate(rv.timeline()):
        if b.error:
            out.append(_fail(
                "turn", "hard", f"{b.role} turn errored",
                where=b.role, detail=str(b.error)[:400], turn=i,
            ))
        elif b.status not in ("completed", "success"):
            out.append(_fail(
                "turn", "hard", f"{b.role} turn {b.status}",
                where=b.role, turn=i,
            ))
        for a in b.acts:
            if a.kind == "obs_err":
                out.append(_fail(
                    "tool", "soft", _first_line(a.text) or "tool error",
                    where=b.role, detail=a.text[:400], turn=i,
                ))

    out.sort(key=lambda f: _SEV_ORDER.get(f["severity"], 3))
    return out


def _first_line(text: str) -> str:
    """The first meaningful line of a tool error, clipped for a list row."""
    for line in (text or "").splitlines():
        s = line.strip()
        if s:
            return s[:120]
    return ""
