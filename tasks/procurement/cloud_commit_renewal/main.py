"""A2a task: ``procurement/cloud_commit_renewal`` — FY27 reseller renewal.

Two roles with opposed commercial interests, two isolated sandboxes (usage
and pricing data ported from agents-last-exam
``computing_math/cost_optimization_1``; the 3 dashboard-image-only findings
are excluded so the setup is fully CLI):

  * ``platform`` — Asha, platform engineering at Halcyon Metrics (buyer,
    assigned). Holds the June usage export, rate card, expiring agreement,
    FIN-OPS-9 policy (rightsizing framework + commitment rules), and a
    competing reseller quote. Must compute the rightsized run-rate, then
    negotiate a 12-month renewal commit + discount and file
    ``usage_baseline.json`` + ``renewal_order.json``.
  * ``accounts`` — Rowan, renewals desk at Stratovault Cloud Services
    (seller). Holds the account billing history, rate card, the internal
    FY27 discount-authority schedule (tiered ceilings + $6k commit floor)
    and account plan (retention priority). Must negotiate within authority
    and file the countersigned ``order_form.json``.

The zone of possible agreement is authored: at the commit band implied by a
correct rightsizing analysis (~$7.5–8.3k/mo) the seller's ceiling is 15%
and the buyer's policy floor is 8%. ``evaluate()`` scores both sandboxes
0.5/0.5 on deterministic criteria: analysis correctness vs the anchor's
recommendation-level reference, discipline against each side's private
policy (commit cap / discount floor; authority ceiling / commit floor),
negotiation efficacy (share of available surplus captured), order
arithmetic, and — the flagship a2a criterion — whether the two parties'
filed records agree term-for-term.

Scoring core exposed as pure functions (``score_platform``,
``score_accounts``) for offline testing against synthetic filings.
"""
from __future__ import annotations

import json
import logging
import shlex
from typing import Any

from tasks.task_spec import load_tasks, part, scored, task_config

logger = logging.getLogger(__name__)

DOMAIN_NAME = "procurement"
TASK_NAME = "cloud_commit_renewal"


# ============================================================================
# Scoring constants (authoritative copies of the staged private policies;
# reference provenance in task-data/.../platform/reference/ and
# .../accounts/reference/)
# ============================================================================

# The 14 rightsizing actions derivable from the two CSVs (anchor's
# savings_summary.csv minus its 3 dashboard-image-only findings).
REFERENCE_ACTIONS: dict[str, str] = {
    "i-0a1b2c3d4e5f": "downsize",            # web-prod-1
    "i-1b2c3d4e5f6g": "downsize",            # web-prod-2
    "i-4e5f6g7h8i9j": "terminate",           # test-server (stopped 45d)
    "i-5f6g7h8i9j0k": "downsize",            # web-eu-1
    "i-7h8i9j0k1l2m": "downsize",            # batch-worker
    "i-8i9j0k1l2m3n": "terminate",           # staging-db (stopped 30d)
    "i-9j0k1l2m3n4o": "downsize",            # analytics-1
    "i-5p6q7r8s9t0u": "terminate",           # dev-sandbox (stopped 60d)
    "i-6q7r8s9t0u1v": "downsize",            # etl-worker
    "db-legacy": "terminate",                # 0 connections, idle 60d
    "db-staging": "change-deployment",       # Multi-AZ -> Single-AZ
    "backup-archive": "change-storage-class",
    "logs-2024": "change-storage-class",
    "compliance-archive": "change-storage-class",
}
REF_RIGHTSIZED_MONTHLY = 7841.0

# Coverage/tolerance knobs: instance-size target selection involves genuine
# judgment (the anchor's own reasons bend its 2x-headroom rule for burstable
# types), so per-action scoring checks the action TYPE only and the totals
# band absorbs sizing judgment.
BASELINE_COVERAGE_MIN = 12          # of the 14 reference actions
RIGHTSIZED_TOTAL_TOL = 0.10

# Seller authority schedule (discount_authority_fy27.md).
AUTHORITY_TIERS: tuple[tuple[float, float], ...] = (
    (9000.0, 20.0), (7500.0, 15.0), (6000.0, 10.0),
)
COMMIT_FLOOR = 6000.0

# Buyer policy (FIN-OPS-9 §3).
BUYER_MIN_DISCOUNT_PCT = 8.0
COMMIT_SLACK = 1.005                # rounding slack on "never exceed"

# Negotiation efficacy: the buyer must capture at least this share of the
# surplus between its 8% policy floor and the seller ceiling at the agreed
# commit tier. (Benchmark-side measure; neither side sees it.)
EFFICACY_CAPTURE = 0.5

ARITH_TOL = 1.0                     # dollars, on effective monthly/annual
MATCH_FIELDS = (
    "agreement_id", "term_months", "committed_monthly_usd", "discount_pct",
)


def authority_cap(commit: float) -> float:
    """Max authorized discount at a commit level; 0.0 below the floor."""
    for threshold, cap in AUTHORITY_TIERS:
        if commit >= threshold:
            return cap
    return 0.0


# ============================================================================
# Pure scoring core (host-side, stdlib only, offline-testable)
# ============================================================================

def within_pct(actual: Any, target: float, pct: float) -> bool:
    try:
        return abs(float(actual) - target) <= abs(target) * pct
    except (TypeError, ValueError):
        return False


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


AGREED_STATUSES = frozenset(
    {"agreed", "accepted", "executed", "confirmed", "renewed", "signed"})
DECLINED_STATUSES = frozenset(
    {"declined", "rejected", "no deal", "no_deal", "walked away"})


def _status_kind(record: dict[str, Any] | None) -> str | None:
    """Normalize the filed ``status`` to 'agreed' / 'declined' / None.

    The docs prescribe 'agreed' / 'declined', but a filer who writes an
    unambiguous synonym ('executed', 'renewed', ...) has still recorded
    the same fact — vocabulary is not what the criteria test.
    """
    raw = str((record or {}).get("status") or "").strip().lower()
    if raw in AGREED_STATUSES:
        return "agreed"
    if raw in DECLINED_STATUSES:
        return "declined"
    return None


def _records_match(order: dict[str, Any], form: dict[str, Any]) -> bool:
    """Both parties filed an agreed deal and the four terms are identical."""
    if _status_kind(order) != "agreed" or _status_kind(form) != "agreed":
        return False
    for key in MATCH_FIELDS:
        a, b = order.get(key), form.get(key)
        an, bn = _num(a), _num(b)
        if an is not None and bn is not None:
            if abs(an - bn) > 0.01:
                return False
        elif not (isinstance(a, str) and isinstance(b, str)
                  and a.strip() and a.strip() == b.strip()):
            return False
    return True


def _order_arithmetic_ok(order: dict[str, Any]) -> bool:
    commit = _num(order.get("committed_monthly_usd"))
    disc = _num(order.get("discount_pct"))
    eff_m = _num(order.get("effective_committed_monthly_usd"))
    eff_a = _num(order.get("effective_annual_committed_usd"))
    if None in (commit, disc, eff_m, eff_a):
        return False
    return (
        _num(order.get("term_months")) == 12.0
        and abs(eff_m - commit * (1 - disc / 100.0)) <= ARITH_TOL
        and abs(eff_a - 12 * eff_m) <= ARITH_TOL
    )


def score_platform(
    baseline: dict[str, Any] | None,
    order: dict[str, Any] | None,
    form: dict[str, Any] | None,
) -> tuple[float, dict[str, Any]]:
    """Six deterministic criteria for the buyer side.

    A missing/unparseable renewal_order.json zeroes the platform side (the
    order is the item's core deliverable); a missing baseline fails only
    the analysis-dependent criteria.
    """
    if not isinstance(order, dict):
        return 0.0, {"reason": "renewal_order.json missing or unparseable"}

    actions = (baseline or {}).get("actions")
    matched = 0
    if isinstance(actions, list):
        seen: set[str] = set()
        for row in actions:
            if not isinstance(row, dict):
                continue
            rid = str(row.get("resource_id") or "").strip()
            act = str(row.get("action") or "").strip().lower()
            if rid in REFERENCE_ACTIONS and rid not in seen \
                    and act == REFERENCE_ACTIONS[rid]:
                seen.add(rid)
                matched += 1

    rightsized = _num((baseline or {}).get("rightsized_monthly_total_usd"))
    current = _num((baseline or {}).get("current_monthly_total_usd"))
    commit = _num(order.get("committed_monthly_usd"))
    disc = _num(order.get("discount_pct"))
    agreed = _status_kind(order) == "agreed"

    commit_ok = (
        agreed and commit is not None and commit > 0
        and rightsized is not None and commit <= rightsized * COMMIT_SLACK
    )
    discount_ok = agreed and disc is not None and disc >= BUYER_MIN_DISCOUNT_PCT

    efficacy_ok = False
    if agreed and commit is not None and disc is not None:
        cap = authority_cap(commit)
        if cap > 0:
            needed = BUYER_MIN_DISCOUNT_PCT + EFFICACY_CAPTURE * (
                cap - BUYER_MIN_DISCOUNT_PCT)
            efficacy_ok = disc >= needed - 1e-9

    criteria = {
        "baseline_actions_correct": matched >= BASELINE_COVERAGE_MIN,
        "rightsized_total_correct": (
            within_pct(rightsized, REF_RIGHTSIZED_MONTHLY, RIGHTSIZED_TOTAL_TOL)
            and current is not None and rightsized is not None
            and current > rightsized
        ),
        "commit_within_policy": bool(commit_ok),
        "discount_within_policy": bool(discount_ok),
        "negotiation_efficacy": bool(efficacy_ok),
        "order_valid_and_consistent": (
            _order_arithmetic_ok(order)
            and isinstance(form, dict) and _records_match(order, form)
        ),
    }
    score = sum(1.0 for ok in criteria.values() if ok) / len(criteria)
    return score, {"criteria": criteria, "matched_actions": matched,
                   "status": order.get("status")}


def score_accounts(
    form: dict[str, Any] | None,
    order: dict[str, Any] | None,
) -> tuple[float, dict[str, Any]]:
    """Six deterministic criteria for the seller side."""
    if not isinstance(form, dict):
        return 0.0, {"reason": "order_form.json missing or unparseable"}

    commit = _num(form.get("committed_monthly_usd"))
    disc = _num(form.get("discount_pct"))
    status = form.get("status")
    agreed = _status_kind(form) == "agreed"
    declined = _status_kind(form) == "declined"

    if agreed and commit is not None and disc is not None:
        authority_ok = disc <= authority_cap(commit) + 1e-9
        floor_ok = commit >= COMMIT_FLOOR
    elif declined:
        # A declination never exceeds authority; whether declining was the
        # RIGHT call shows up in deal_closed (the ZOPA is nonempty by
        # construction, so a compliant close was always available).
        authority_ok = True
        floor_ok = True
    else:
        authority_ok = False
        floor_ok = False

    required_present = all(
        form.get(k) not in (None, "") for k in (
            "agreement_id", "term_months", "term_start", "term_end",
            "committed_monthly_usd", "discount_pct",
            "effective_committed_monthly_usd",
            "effective_annual_committed_usd", "status", "counterparty_agent",
        )
    ) if agreed else (declined and bool(form.get("declined_reason")))

    criteria = {
        "authority_respected": bool(authority_ok),
        "commit_floor_respected": bool(floor_ok),
        "deal_closed": bool(
            agreed and isinstance(order, dict)
            and _status_kind(order) == "agreed"
        ),
        "form_arithmetic_correct": agreed and _order_arithmetic_ok(form),
        "records_consistent": (
            agreed and isinstance(order, dict) and _records_match(order, form)
        ),
        "form_complete": bool(required_present),
    }
    score = sum(1.0 for ok in criteria.values() if ok) / len(criteria)
    return score, {"criteria": criteria, "status": status}


def load():
    return load_tasks(__file__)


# ============================================================================
# Lifecycle hooks
# ============================================================================

_PLATFORM_STAGED_SPOT_CHECKS = (
    "input/usage_export_2026_06.csv",
    "input/price_list.csv",
    "input/procurement_policy.md",
    "input/current_agreement.md",
    "input/quote_corvess_fy27.md",
    "input/renewal_order_template.json",
    "software/inspect_usage.sh",
)
_ACCOUNTS_STAGED_SPOT_CHECKS = (
    "input/account_billing_fy26.csv",
    "input/price_list.csv",
    "input/discount_authority_fy27.md",
    "input/account_plan_halcyon.md",
    "input/order_form_template.json",
    "software/check_order_form.py",
)


async def start(task_cfg, sandboxes):
    """Assert per-role staging landed; ready both output dirs."""
    cfg = task_config(__file__)
    for role, rel_paths in (
        ("platform", _PLATFORM_STAGED_SPOT_CHECKS),
        ("accounts", _ACCOUNTS_STAGED_SPOT_CHECKS),
    ):
        sb = sandboxes[role]
        for rel in rel_paths:
            path = f"{cfg.task_dir}/{rel}"
            if not await sb.exists(path):
                raise RuntimeError(
                    f"staged input missing in {role} sandbox: {path}. Check "
                    f"task-data/{DOMAIN_NAME}/{TASK_NAME}/base/{role}/ on "
                    "the host and the local: staging config."
                )
        await sb.mkdir(cfg.remote_output_dir)
        # Exec bits are not preserved by the staging transport.
        await sb.run_command(f"chmod +x {shlex.quote(cfg.software_dir)}/* || true")

    await sandboxes["platform"].rm([cfg.baseline_path, cfg.renewal_order_path])
    await sandboxes["accounts"].rm([cfg.order_form_path])
    logger.info("[cloud_commit_renewal] staged inputs verified; output dirs ready")


async def _read_json(sandbox, path: str) -> dict[str, Any] | None:
    try:
        return json.loads(await sandbox.read_text(path))
    except FileNotFoundError:
        logger.info("[cloud_commit_renewal] missing: %s", path)
        return None
    except (json.JSONDecodeError, RuntimeError) as exc:
        logger.info("[cloud_commit_renewal] unparseable JSON at %s: %s", path, exc)
        return None


async def evaluate(task_cfg, sandboxes) -> dict[str, Any]:
    """Score both sandboxes; 0.5*platform + 0.5*accounts, with its rubric."""
    cfg = task_config(__file__)

    baseline = await _read_json(sandboxes["platform"], cfg.baseline_path)
    order = await _read_json(sandboxes["platform"], cfg.renewal_order_path)
    form = await _read_json(sandboxes["accounts"], cfg.order_form_path)

    platform_score, platform_detail = score_platform(baseline, order, form)
    accounts_score, accounts_detail = score_accounts(form, order)

    logger.info("[cloud_commit_renewal] platform subscore=%.4f detail=%s",
                platform_score, platform_detail)
    logger.info("[cloud_commit_renewal] accounts subscore=%.4f detail=%s",
                accounts_score, accounts_detail)
    combined = 0.5 * platform_score + 0.5 * accounts_score
    logger.info("[cloud_commit_renewal] combined score=%.4f "
                "(platform=%.4f, accounts=%.4f)",
                combined, platform_score, accounts_score)
    return scored(combined, [
        part("platform", platform_score, weight=0.5, **platform_detail),
        part("accounts", accounts_score, weight=0.5, **accounts_detail),
    ])
