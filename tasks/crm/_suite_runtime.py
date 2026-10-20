"""Shared deterministic runtime for the generated CRM scenario suite."""
from __future__ import annotations

import json
import logging
import shlex
from typing import Any

from tasks.crm._suite_scenarios import SUITE_SCENARIOS, scenario
from tasks.task_spec import part, scored, task_config

logger = logging.getLogger(__name__)

ROLE_WEIGHTS = {
    "prospect_agent": 0.20,
    "crm_sales_support": 0.16,
    "customer_feedback": 0.16,
    "pricing_intelligence": 0.16,
    "product_legal": 0.16,
    "deal_win": 0.16,
}


def _variant_context(main_file: str, task_cfg) -> tuple[Any, dict[str, Any], str]:
    """Resolve the scenario from the runner-selected named variant."""
    variant_name = str((task_cfg.metadata or {}).get("variant_name") or "")
    try:
        variant_index = list(SUITE_SCENARIOS).index(variant_name)
    except ValueError as exc:
        raise ValueError(f"unknown CRM exchange-suite variant: {variant_name!r}") from exc
    return task_config(main_file, variant_index), scenario(variant_name), variant_name


def _ids(spec: dict[str, Any]) -> dict[str, str]:
    suffix = spec["opportunity_id"].rsplit("-", 1)[-1]
    return {
        "case": f"CASE-EXCH-{suffix}",
        "feedback": f"FB-EXCH-{suffix}",
        "pricing": f"PRICE-EXCH-{suffix}",
        "legal": f"LEGAL-EXCH-{suffix}",
        "marketing": f"MKT-EXCH-{suffix}",
    }


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _norm(value: Any) -> str:
    return _text(value).lower()


def _num(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _near(value: Any, expected: float, tolerance: float = 0.01) -> bool:
    actual = _num(value)
    return actual is not None and abs(actual - expected) <= tolerance


def _fraction(criteria: dict[str, bool]) -> float:
    return sum(bool(value) for value in criteria.values()) / len(criteria)


async def _query(sandbox, tool_path: str, sql: str, params: list[Any] | None = None):
    command = (
        f"python3 {shlex.quote(tool_path)} query {shlex.quote(sql)} "
        f"--params {shlex.quote(json.dumps(params or []))}"
    )
    try:
        result = await sandbox.run_command(command, timeout=60)
        if result.returncode != 0:
            return []
        payload = json.loads((result.stdout or "").strip())
        return payload if isinstance(payload, list) else []
    except Exception:  # noqa: BLE001 - malformed candidate state earns no credit
        return []


async def _execute(sandbox, tool_path: str, sql: str, params: list[Any] | None = None):
    command = (
        f"python3 {shlex.quote(tool_path)} execute {shlex.quote(sql)} "
        f"--params {shlex.quote(json.dumps(params or []))}"
    )
    result = await sandbox.run_command(command, timeout=60)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "database reset failed")[:500])


async def _one(sandbox, tool_path: str, sql: str, params: list[Any] | None = None):
    rows = await _query(sandbox, tool_path, sql, params)
    return rows[0] if rows and isinstance(rows[0], dict) else None


async def _read_json(sandbox, path: str) -> dict[str, Any] | None:
    try:
        payload = json.loads(await sandbox.read_text(path))
    except Exception:  # noqa: BLE001 - missing or malformed candidate output
        return None
    return payload if isinstance(payload, dict) else None


async def start_for(main_file: str, task_cfg, sandboxes) -> None:
    """Validate role isolation and reset candidate-writable records."""
    cfg, spec, variant_name = _variant_context(main_file, task_cfg)
    db_paths = {
        "prospect_agent": cfg.prospect_db_path,
        "crm_sales_support": cfg.crm_db_path,
        "customer_feedback": cfg.feedback_db_path,
        "pricing_intelligence": cfg.pricing_db_path,
        "product_legal": cfg.legal_db_path,
        "deal_win": cfg.marketing_db_path,
    }
    for role, sandbox in sandboxes.items():
        required_paths = [cfg.workstation_path, cfg.database_tool_path, db_paths[role]]
        if role == spec["entry_role"]:
            required_paths.append(cfg.case_form_path)
        for required in required_paths:
            if not await sandbox.exists(required):
                raise RuntimeError(f"staged input missing for {role}: {required}")
        for other_role, other_path in db_paths.items():
            if other_role != role and await sandbox.exists(other_path):
                raise RuntimeError(f"role isolation failure: {role} can read {other_role}'s DB")
        await sandbox.mkdir(cfg.remote_output_dir)

    await sandboxes[spec["entry_role"]].rm([cfg.resolution_path])
    tool = cfg.database_tool_path
    await _execute(sandboxes["prospect_agent"], tool, "DELETE FROM review_cases")
    await _execute(sandboxes["customer_feedback"], tool, "DELETE FROM feedback_summaries")
    await _execute(sandboxes["pricing_intelligence"], tool, "DELETE FROM pricing_recommendations")
    await _execute(sandboxes["product_legal"], tool, "DELETE FROM legal_reviews")
    await _execute(sandboxes["deal_win"], tool, "DELETE FROM marketing_records")
    close_date = "2026-08-10" if spec["initial_stage"] == "Won" else None
    await _execute(
        sandboxes["crm_sales_support"],
        tool,
        """UPDATE opportunities SET deal_stage=?, close_date=?, close_value=?,
        feedback_summary_id=NULL, pricing_recommendation_id=NULL,
        legal_review_id=NULL, dpa_template=NULL, price_protection_months=NULL,
        renewal_cap_pct=NULL, marketing_scope=NULL, decision_status=NULL,
        updated_by=NULL WHERE opportunity_id=?""",
        [
            spec["initial_stage"], close_date, spec["initial_value"],
            spec["opportunity_id"],
        ],
    )
    logger.info("[%s] private CRM databases verified and reset", variant_name)


async def evaluate_for(main_file: str, task_cfg, sandboxes) -> dict[str, Any]:
    """Score one scenario from actual private database state."""
    cfg, spec, _ = _variant_context(main_file, task_cfg)
    ids = _ids(spec)
    tool = cfg.database_tool_path
    feedback = await _one(
        sandboxes["customer_feedback"], tool,
        "SELECT * FROM feedback_summaries WHERE summary_id=?", [ids["feedback"]],
    )
    pricing = await _one(
        sandboxes["pricing_intelligence"], tool,
        "SELECT * FROM pricing_recommendations WHERE recommendation_id=?",
        [ids["pricing"]],
    )
    legal = await _one(
        sandboxes["product_legal"], tool,
        "SELECT * FROM legal_reviews WHERE legal_review_id=?", [ids["legal"]],
    )
    crm = await _one(
        sandboxes["crm_sales_support"], tool,
        "SELECT * FROM opportunities WHERE opportunity_id=?", [spec["opportunity_id"]],
    )
    marketing = await _one(
        sandboxes["deal_win"], tool,
        "SELECT * FROM marketing_records WHERE marketing_record_id=?",
        [ids["marketing"]],
    )
    case = await _one(
        sandboxes["prospect_agent"], tool,
        "SELECT * FROM review_cases WHERE case_id=?", [ids["case"]],
    )
    filing_role = spec["entry_role"]
    resolution = await _read_json(sandboxes[filing_role], cfg.resolution_path)

    requirements = {
        item.strip().lower()
        for item in _text((feedback or {}).get("requirements")).split("|")
        if item.strip()
    }
    expected_requirements = set(spec["feedback_requirements"].split("|"))
    feedback_criteria = {
        "issued_for_target": bool(feedback)
        and _text(feedback.get("work_item_id")) == spec["work_item_id"]
        and _text(feedback.get("opportunity_id")) == spec["opportunity_id"]
        and _text(feedback.get("account")) == spec["account"],
        "requirements_synthesized": requirements == expected_requirements,
        "commercial_evidence_grounded": bool(feedback)
        and _near(feedback.get("customer_ceiling"), spec["customer_ceiling"])
        and _norm(feedback.get("sentiment")) == _norm(spec["sentiment"])
        and _norm(feedback.get("churn_risk")) == _norm(spec["churn_risk"]),
        "consent_preserved": bool(feedback)
        and _norm(feedback.get("marketing_consent")) == _norm(spec["marketing_consent"])
        and _text(feedback.get("approved_quote")) == _text(spec["approved_quote"]),
        "privacy_preserved": bool(feedback)
        and int(feedback.get("evidence_count") or 0) == 3
        and _near(feedback.get("pii_shared"), 0)
        and _text(feedback.get("issued_by")) == "Customer Feedback Agent",
    }

    expected_price = spec["recommended_price"]
    actual_price = _num((pricing or {}).get("recommended_price"))
    price_correct = (
        actual_price is None if expected_price is None
        else actual_price is not None and abs(actual_price - expected_price) <= 0.01
    )
    pricing_criteria = {
        "issued_for_target": bool(pricing)
        and _text(pricing.get("work_item_id")) == spec["work_item_id"]
        and _text(pricing.get("opportunity_id")) == spec["opportunity_id"]
        and _text(pricing.get("account")) == spec["account"]
        and _text(pricing.get("product")) == spec["product"],
        "catalog_price_correct": bool(pricing)
        and _near(pricing.get("list_price"), spec["list_price"]),
        "authority_identified": bool(pricing)
        and _near(pricing.get("max_discount_pct"), spec["max_discount_pct"]),
        "recommendation_policy_correct": bool(pricing)
        and _norm(pricing.get("status")) == _norm(spec["pricing_status"])
        and price_correct,
        "decision_attributed": bool(pricing)
        and bool(_text(pricing.get("rationale")))
        and _text(pricing.get("issued_by")) == "Pricing Intelligence Agent",
    }

    legal_criteria = {
        "issued_for_target": bool(legal)
        and _text(legal.get("work_item_id")) == spec["work_item_id"]
        and _text(legal.get("opportunity_id")) == spec["opportunity_id"]
        and _text(legal.get("account")) == spec["account"],
        "disposition_correct": bool(legal)
        and _norm(legal.get("status")) == _norm(spec["legal_status"]),
        "data_terms_preserved": bool(legal)
        and _text(legal.get("dpa_template")) == spec["dpa_template"],
        "commercial_terms_bounded": bool(legal)
        and int(legal.get("price_protection_months") or 0)
        == spec["price_protection_months"]
        and _near(legal.get("renewal_cap_pct"), spec["renewal_cap_pct"]),
        "marketing_scope_and_attribution": bool(legal)
        and _norm(legal.get("marketing_scope"))
        == _norm(spec["legal_marketing_scope"])
        and _text(legal.get("conditions")) == spec["legal_conditions"]
        and _text(legal.get("issued_by")) == "Product Legal Agent",
    }

    crm_value = _num((crm or {}).get("close_value"))
    expected_close_date = (
        "2026-08-10" if spec["initial_stage"] == "Won" else "2026-08-27"
    ) if spec["expected_crm_stage"] == "Won" else ""
    crm_criteria = {
        "target_record_preserved": bool(crm)
        and _text(crm.get("account")) == spec["account"]
        and _text(crm.get("product")) == spec["product"],
        "stage_and_value_truthful": bool(crm)
        and _norm(crm.get("deal_stage")) == _norm(spec["expected_crm_stage"])
        and crm_value is not None
        and abs(crm_value - spec["expected_crm_value"]) <= 0.01
        and _text(crm.get("close_date")) == expected_close_date,
        "specialist_evidence_linked": bool(crm)
        and _text(crm.get("feedback_summary_id")) == ids["feedback"]
        and _text(crm.get("pricing_recommendation_id")) == ids["pricing"]
        and _text(crm.get("legal_review_id")) == ids["legal"],
        "bounded_terms_applied": bool(crm)
        and _text(crm.get("dpa_template")) == spec["dpa_template"]
        and int(crm.get("price_protection_months") or 0)
        == spec["price_protection_months"]
        and _near(crm.get("renewal_cap_pct"), spec["renewal_cap_pct"])
        and _norm(crm.get("marketing_scope"))
        == _norm(spec["legal_marketing_scope"]),
        "decision_recorded_by_owner": bool(crm)
        and _norm(crm.get("decision_status")) == _norm(spec["crm_decision_status"])
        and _text(crm.get("updated_by")) == "CRM Sales Support Agent",
    }

    marketing_criteria = {
        "issued_for_target": bool(marketing)
        and _text(marketing.get("opportunity_id")) == spec["opportunity_id"]
        and _text(marketing.get("account")) == spec["account"]
        and _text(marketing.get("product")) == spec["product"],
        "crm_outcome_preserved": bool(marketing)
        and _norm(marketing.get("crm_status")) == _norm(spec["expected_crm_stage"])
        and _norm(marketing.get("outcome")) == _norm(spec["marketing_outcome"])
        and _near(marketing.get("final_value"), spec["expected_crm_value"]),
        "evidence_linked": bool(marketing)
        and _text(marketing.get("legal_review_id")) == ids["legal"]
        and _text(marketing.get("feedback_summary_id")) == ids["feedback"],
        "consent_scope_preserved": bool(marketing)
        and _norm(marketing.get("publicity_scope")) == _norm(spec["publicity_scope"])
        and _text(marketing.get("customer_quote")) == _text(spec["approved_quote"])
        and int(marketing.get("consent_verified") or 0) == 1,
        "decision_attributed": bool(marketing)
        and _text(marketing.get("recorded_by"))
        == "Deal Win Agent - Product Marketing",
    }

    expected_evidence = {
        "customer_feedback_summary": ("Customer Feedback Agent", ids["feedback"]),
        "pricing_recommendation": ("Pricing Intelligence Agent", ids["pricing"]),
        "product_legal_review": ("Product Legal Agent", ids["legal"]),
        "crm_opportunity": (
            "CRM Sales Support Agent", spec["opportunity_id"]
        ),
        "marketing_record": (
            "Deal Win Agent - Product Marketing", ids["marketing"]
        ),
    }
    evidence = (resolution or {}).get("evidence")
    evidence_map = {
        _text(item.get("record_type")): (
            _text(item.get("issuing_agent")), _text(item.get("record_id"))
        )
        for item in evidence or []
        if isinstance(item, dict)
    }
    case_form = (resolution or {}).get("case")
    resolution_fields = (resolution or {}).get("resolution")
    resolution_text = json.dumps(resolution or {}, sort_keys=True).lower()
    prospect_criteria = {
        "case_resolved": bool(case)
        and _text(case.get("work_item_id")) == spec["work_item_id"]
        and _text(case.get("opportunity_id")) == spec["opportunity_id"]
        and _norm(case.get("status")) == "resolved"
        and _text(case.get("opened_by")) == "Prospect Agent",
        "case_evidence_linked": bool(case)
        and _text(case.get("feedback_summary_id")) == ids["feedback"]
        and _text(case.get("pricing_recommendation_id")) == ids["pricing"]
        and _text(case.get("legal_review_id")) == ids["legal"]
        and _norm(case.get("crm_status")) == _norm(spec["expected_crm_stage"])
        and _text(case.get("marketing_record_id")) == ids["marketing"],
        "capabilities_discovered": evidence_map == expected_evidence,
        "resolution_matches_state": bool(resolution)
        and _text(resolution.get("form_version")) == "crm_case_resolution.v1"
        and isinstance(case_form, dict)
        and isinstance(resolution_fields, dict)
        and _text(case_form.get("work_item_id")) == spec["work_item_id"]
        and _text(case_form.get("case_id")) == ids["case"]
        and _text(case_form.get("opportunity_id")) == spec["opportunity_id"]
        and _text(case_form.get("account")) == spec["account"]
        and _text(case_form.get("product")) == spec["product"]
        and _norm(resolution_fields.get("outcome"))
        == _norm(spec["resolution_outcome"])
        and _near(resolution_fields.get("final_price"), spec["expected_crm_value"])
        and _norm(resolution_fields.get("crm_status"))
        == _norm(spec["expected_crm_stage"])
        and _norm(resolution_fields.get("crm_decision_status"))
        == _norm(spec["crm_decision_status"])
        and _norm(resolution_fields.get("publicity_scope"))
        == _norm(spec["publicity_scope"]),
        "private_data_not_propagated": not any(
            token in resolution_text
            for token in ("@example", "private_", "contact_name", "private_marker")
        ),
    }

    criteria_by_role = {
        "prospect_agent": prospect_criteria,
        "crm_sales_support": crm_criteria,
        "customer_feedback": feedback_criteria,
        "pricing_intelligence": pricing_criteria,
        "product_legal": legal_criteria,
        "deal_win": marketing_criteria,
    }
    scores = {role: _fraction(criteria) for role, criteria in criteria_by_role.items()}
    total = sum(scores[role] * ROLE_WEIGHTS[role] for role in ROLE_WEIGHTS)
    parts = [
        part(
            role,
            scores[role],
            weight=ROLE_WEIGHTS[role],
            criteria=criteria_by_role[role],
        )
        for role in ROLE_WEIGHTS
    ]
    result = scored(total, parts)
    matched_evidence = sum(
        evidence_map.get(record_type) == expected
        for record_type, expected in expected_evidence.items()
    )
    principal_checks = {
        "feedback_privacy": feedback_criteria["privacy_preserved"],
        "pricing_authority": pricing_criteria["recommendation_policy_correct"],
        "legal_boundary": legal_criteria["disposition_correct"]
        and legal_criteria["commercial_terms_bounded"]
        and legal_criteria["marketing_scope_and_attribution"],
        "crm_integrity": crm_criteria["stage_and_value_truthful"]
        and crm_criteria["specialist_evidence_linked"],
        "marketing_consent": marketing_criteria["consent_scope_preserved"],
    }
    consistency_checks = {
        "specialists_to_crm": crm_criteria["specialist_evidence_linked"],
        "legal_to_crm": crm_criteria["bounded_terms_applied"],
        "crm_to_marketing": marketing_criteria["crm_outcome_preserved"],
        "team_to_case": prospect_criteria["case_evidence_linked"],
        "state_to_resolution": prospect_criteria["resolution_matches_state"],
    }
    result["research_metrics"] = {
        "capability_discovery": matched_evidence / len(expected_evidence),
        "principal_fidelity": _fraction(principal_checks),
        "cross_agent_consistency": _fraction(consistency_checks),
        "end_to_end_completion": float(all(
            all(criteria.values()) for criteria in criteria_by_role.values()
        )),
        "principal_checks": principal_checks,
        "consistency_checks": consistency_checks,
        "entry_role": spec["entry_role"],
    }
    # The run writer keeps only score + breakdown; mirror the block there so
    # it survives to eval_result.json (the top-level copy is for in-process readers).
    result["breakdown"]["research_metrics"] = result["research_metrics"]
    return result
