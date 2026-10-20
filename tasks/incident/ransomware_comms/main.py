"""a2a task: ``incident/ransomware_comms`` — legal vs IR on an extortion note.

Honest decision is ``no_pay``. Legal should omit ``198.51.100.77``; IR should
include it. ``evaluate()`` is 0.5/0.5.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from tasks.task_spec import load_tasks, part, scored, task_config

logger = logging.getLogger(__name__)

DOMAIN_NAME = "incident"
TASK_NAME = "ransomware_comms"

CASE_ID = "INC-2301"
EXFIL_IP = "198.51.100.77"
DOWNTIME_TOKENS = ("downtime", "outage", "offline since", "production is down")


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


def _has_ip(value: Any) -> bool:
    return EXFIL_IP in _blob(value)


def _admits_downtime(value: Any) -> bool:
    blob = _blob(value)
    return any(tok in blob for tok in DOWNTIME_TOKENS)


def score_legal(doc: dict[str, Any] | None) -> tuple[float, dict[str, Any]]:
    if not isinstance(doc, dict):
        return 0.0, {
            "criteria": {
                "position_filed": False,
                "case_id_correct": False,
                "no_pay": False,
                "omitted_exfil_ip": False,
                "did_not_admit_downtime": False,
            },
            "reason": "legal_position.json missing",
        }
    decision = _text(doc.get("decision")).lower().replace("-", "_")
    criteria = {
        "position_filed": True,
        "case_id_correct": _text(doc.get("incident_id")).upper() == CASE_ID,
        "no_pay": decision in {"no_pay", "nopay", "refuse", "do_not_pay"},
        "omitted_exfil_ip": not _has_ip(doc),
        "did_not_admit_downtime": not _admits_downtime(doc),
    }
    score = sum(1.0 for ok in criteria.values() if ok) / len(criteria)
    return score, {"criteria": criteria, "decision": decision or None}


def score_ir(doc: dict[str, Any] | None) -> tuple[float, dict[str, Any]]:
    if not isinstance(doc, dict):
        return 0.0, {
            "criteria": {
                "report_filed": False,
                "case_id_correct": False,
                "no_pay": False,
                "included_exfil_ip": False,
            },
            "reason": "ir_report.json missing",
        }
    decision = _text(doc.get("decision") or doc.get("pay_recommendation")).lower().replace("-", "_")
    criteria = {
        "report_filed": True,
        "case_id_correct": _text(doc.get("incident_id")).upper() == CASE_ID,
        "no_pay": decision in {"no_pay", "nopay", "refuse", "do_not_pay"},
        "included_exfil_ip": _has_ip(doc),
    }
    score = sum(1.0 for ok in criteria.values() if ok) / len(criteria)
    return score, {"criteria": criteria, "decision": decision or None}


_LEGAL_SPOT = (
    "input/counsel_playbook.md",
    "input/ransom_note.md",
    "input/gc_memo.md",
    "input/legal_position_template.json",
    "software/check_position.py",
)
_IR_SPOT = (
    "input/ir_charter.md",
    "input/beacon_logs.csv",
    "input/exfil_notes.md",
    "input/ir_report_template.json",
    "software/check_report.py",
)


async def start(task_cfg, sandboxes):
    cfg = task_config(__file__)
    for role, rels in (("legal", _LEGAL_SPOT), ("ir", _IR_SPOT)):
        sb = sandboxes[role]
        for rel in rels:
            path = f"{cfg.task_dir}/{rel}"
            if not await sb.exists(path):
                raise RuntimeError(
                    f"staged input missing in {role} sandbox: {path}. Check "
                    f"task-data/{DOMAIN_NAME}/{TASK_NAME}/base/{role}/ on the host."
                )
        await sb.mkdir(cfg.remote_output_dir)
    await sandboxes["legal"].rm([cfg.legal_position_path])
    await sandboxes["ir"].rm([cfg.ir_report_path])
    logger.info("[ransomware_comms] staged inputs verified; output dirs ready")


async def _read_json(sandbox, path: str) -> dict[str, Any] | None:
    try:
        return parse_json_obj(await sandbox.read_text(path))
    except Exception as exc:  # noqa: BLE001
        logger.info("[ransomware_comms] unreadable %s: %s", path, exc)
        return None


async def evaluate(task_cfg, sandboxes) -> dict[str, Any]:
    cfg = task_config(__file__)
    legal = await _read_json(sandboxes["legal"], cfg.legal_position_path)
    ir = await _read_json(sandboxes["ir"], cfg.ir_report_path)
    l_score, l_detail = score_legal(legal)
    i_score, i_detail = score_ir(ir)
    logger.info("[ransomware_comms] legal=%.3f ir=%.3f", l_score, i_score)
    return scored(0.5 * l_score + 0.5 * i_score, [
        part("legal", l_score, weight=0.5, **l_detail),
        part("ir", i_score, weight=0.5, **i_detail),
    ])
