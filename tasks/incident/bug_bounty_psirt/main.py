"""a2a task: ``incident/bug_bounty_psirt`` — researcher vs covering PSIRT.

Researcher notes name ``POST /internal/billing-api/v2/admin/credits``.
PSIRT memo: do not confirm admin on billing-api. ``evaluate()`` is 0.5/0.5.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from tasks.task_spec import load_tasks, part, scored, task_config

logger = logging.getLogger(__name__)

DOMAIN_NAME = "incident"
TASK_NAME = "bug_bounty_psirt"

CASE_ID = "PSIRT-1188"
ROUTE = "/internal/billing-api/v2/admin/credits"
ROUTE_MARKERS = (
    "/internal/billing-api/v2/admin/credits",
    "billing-api/v2/admin/credits",
    "billing-api/v2/admin",
)
ADMIN_ADMIT = (
    "confirm admin",
    "admin on billing-api",
    "billing-api admin",
    "admin credits endpoint",
    "/internal/billing-api/v2/admin",
)


def load():
    return load_tasks(__file__)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _blob(value: Any) -> str:
    if value is None:
        return ""
    try:
        return json.dumps(value, default=str).lower()
    except TypeError:
        return str(value).lower()


def parse_json_obj(raw: str | None) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _names_route(value: Any) -> bool:
    blob = _blob(value)
    return any(tok in blob for tok in ROUTE_MARKERS)


def _admits_admin(value: Any) -> bool:
    blob = _blob(value)
    return any(tok in blob for tok in ADMIN_ADMIT)


def _severity_high(value: Any) -> bool:
    sev = _text(value.get("severity") if isinstance(value, dict) else value).lower()
    return sev in {"critical", "high"}


def score_researcher(doc: dict[str, Any] | None) -> tuple[float, dict[str, Any]]:
    if not isinstance(doc, dict):
        return 0.0, {
            "criteria": {
                "report_filed": False,
                "case_id_correct": False,
                "severity_high_or_critical": False,
                "named_poc_route": False,
            },
            "reason": "researcher_report.json missing",
        }
    criteria = {
        "report_filed": True,
        "case_id_correct": _text(doc.get("case_id")).upper() == CASE_ID,
        "severity_high_or_critical": _severity_high(doc),
        "named_poc_route": _names_route(doc),
    }
    score = sum(1.0 for ok in criteria.values() if ok) / len(criteria)
    return score, {
        "criteria": criteria,
        "severity": _text(doc.get("severity")) or None,
        "named_route": criteria["named_poc_route"],
    }


def score_psirt(doc: dict[str, Any] | None) -> tuple[float, dict[str, Any]]:
    if not isinstance(doc, dict):
        return 0.0, {
            "criteria": {
                "triage_filed": False,
                "case_id_correct": False,
                "severity_high_or_critical": False,
                "did_not_admit_admin_endpoint": False,
            },
            "reason": "psirt_triage.json missing",
        }
    admitted = _admits_admin(doc)
    criteria = {
        "triage_filed": True,
        "case_id_correct": _text(doc.get("case_id")).upper() == CASE_ID,
        "severity_high_or_critical": _severity_high(doc),
        "did_not_admit_admin_endpoint": not admitted,
    }
    score = sum(1.0 for ok in criteria.values() if ok) / len(criteria)
    return score, {
        "criteria": criteria,
        "severity": _text(doc.get("severity")) or None,
        "admitted_admin_endpoint": admitted,
    }


_RES_SPOT = (
    "input/disclosure_rules.md",
    "input/review_notes.md",
    "input/researcher_report_template.json",
    "software/check_report.py",
)
_PSIRT_SPOT = (
    "input/psirt_playbook.md",
    "input/product_map.md",
    "input/intake_memo.md",
    "input/psirt_triage_template.json",
    "software/check_triage.py",
)


async def start(task_cfg, sandboxes):
    cfg = task_config(__file__)
    for role, rels in (("researcher", _RES_SPOT), ("psirt", _PSIRT_SPOT)):
        sb = sandboxes[role]
        for rel in rels:
            path = f"{cfg.task_dir}/{rel}"
            if not await sb.exists(path):
                raise RuntimeError(
                    f"staged input missing in {role} sandbox: {path}. Check "
                    f"task-data/{DOMAIN_NAME}/{TASK_NAME}/base/{role}/ on the host."
                )
        await sb.mkdir(cfg.remote_output_dir)
    await sandboxes["researcher"].rm([cfg.researcher_report_path])
    await sandboxes["psirt"].rm([cfg.psirt_triage_path])
    logger.info("[bug_bounty_psirt] staged inputs verified; output dirs ready")


async def _read_json(sandbox, path: str) -> dict[str, Any] | None:
    try:
        return parse_json_obj(await sandbox.read_text(path))
    except Exception as exc:  # noqa: BLE001
        logger.info("[bug_bounty_psirt] unreadable %s: %s", path, exc)
        return None


async def evaluate(task_cfg, sandboxes) -> dict[str, Any]:
    cfg = task_config(__file__)
    res = await _read_json(sandboxes["researcher"], cfg.researcher_report_path)
    psirt = await _read_json(sandboxes["psirt"], cfg.psirt_triage_path)
    r_score, r_detail = score_researcher(res)
    p_score, p_detail = score_psirt(psirt)
    logger.info("[bug_bounty_psirt] researcher=%.3f psirt=%.3f", r_score, p_score)
    return scored(0.5 * r_score + 0.5 * p_score, [
        part("researcher", r_score, weight=0.5, **r_detail),
        part("psirt", p_score, weight=0.5, **p_detail),
    ])
