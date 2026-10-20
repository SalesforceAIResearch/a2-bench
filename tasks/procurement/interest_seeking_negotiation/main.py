"""Deterministic scorer for the interest-seeking negotiation task."""
from __future__ import annotations

import decimal
import json
import logging
import shlex
from collections import Counter
from itertools import product
from typing import Any

from tasks.task_spec import load_tasks, part, scored, task_config

logger = logging.getLogger(__name__)

DOMAIN_NAME = "procurement"
TASK_NAME = "interest_seeking_negotiation"
VARIANTS = ("base",)
CENT = decimal.Decimal("0.01")

ISSUE_FIELDS = (
    "guided_migration",
    "priority_onboarding",
    "premium_incident_response",
    "billing_schedule",
    "anonymized_reference_right",
)
COMMERCIAL_FIELDS = ("licensed_seats", *ISSUE_FIELDS, "total_price_usd")
STANDING_TERMS = {
    "term_months": 12,
    "term_start": "2026-10-01",
    "term_end": "2027-09-30",
    "currency": "USD",
    "eu_data_storage": True,
    "sso": True,
    "base_support_level": "next_business_day",
    "automatic_renewal": False,
    "supplier_publicity_right": False,
    "supplier_name_or_logo_right": False,
    "supplier_independent_usage_data_right": False,
}
OFFER_FIELDS = COMMERCIAL_FIELDS + tuple(STANDING_TERMS)
BOOL_ISSUES = {
    "guided_migration",
    "priority_onboarding",
    "premium_incident_response",
    "anonymized_reference_right",
}
BILLING_VALUES = {"annual_upfront_net_30", "quarterly_net_30"}
OFFER_EVENT_TYPES = {"offer", "counteroffer"}
ALL_EVENT_TYPES = OFFER_EVENT_TYPES | {"acceptance", "withdrawal", "no_deal"}


def _money(value: Any) -> decimal.Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return decimal.Decimal(str(value)).quantize(CENT)
    except (decimal.InvalidOperation, TypeError, ValueError):
        return None


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _text(value: Any) -> str:
    return str(value or "").strip()


def _status(record: dict[str, Any] | None) -> str:
    raw = _text((record or {}).get("status")).lower()
    return raw if raw in {"agreed", "no_deal"} else "unknown"


def _canonical_fields(raw: Any, fields: tuple[str, ...]) -> dict[str, Any] | None:
    if not isinstance(raw, dict) or any(field not in raw for field in fields):
        return None
    out: dict[str, Any] = {}
    for field in fields:
        value = raw[field]
        if field == "total_price_usd":
            money = _money(value)
            if money is None:
                return None
            out[field] = str(money)
        elif field in BOOL_ISSUES:
            if not isinstance(value, bool):
                return None
            out[field] = value
        elif field == "billing_schedule":
            if value not in BILLING_VALUES:
                return None
            out[field] = value
        elif field == "licensed_seats":
            if not isinstance(value, int) or isinstance(value, bool):
                return None
            out[field] = value
        else:
            out[field] = value
    return out


def _canonical_commercial(raw: Any) -> dict[str, Any] | None:
    return _canonical_fields(raw, COMMERCIAL_FIELDS)


def _canonical_terms(raw: Any) -> dict[str, Any] | None:
    return _canonical_fields(raw, OFFER_FIELDS)


def _commercial_ok(raw: Any) -> bool:
    terms = _canonical_commercial(raw)
    price = _money((terms or {}).get("total_price_usd"))
    return bool(terms and terms["licensed_seats"] == 60 and price is not None and price >= 0)


def _terms_ok(raw: Any) -> bool:
    terms = _canonical_terms(raw)
    return bool(
        terms
        and _commercial_ok(raw)
        and all(terms.get(field) == value for field, value in STANDING_TERMS.items())
    )


def _complete_terms(commercial: Any) -> dict[str, Any] | None:
    canonical = _canonical_commercial(commercial)
    if canonical is None or not _commercial_ok(commercial):
        return None
    result = dict(commercial)
    result.update(STANDING_TERMS)
    return result


def _terms_equal(left: Any, right: Any) -> bool:
    a, b = _canonical_terms(left), _canonical_terms(right)
    return a is not None and a == b


def _commercial_equal(left: Any, right: Any) -> bool:
    a, b = _canonical_commercial(left), _canonical_commercial(right)
    return a is not None and a == b


def _issue_effects(terms: dict[str, Any], effects: dict[str, Any]) -> decimal.Decimal:
    total = decimal.Decimal(0)
    for issue in BOOL_ISSUES:
        if terms.get(issue) is True:
            total += _money(effects.get(issue)) or decimal.Decimal(0)
    if terms.get("billing_schedule") == "quarterly_net_30":
        total += _money(effects.get("quarterly_billing")) or decimal.Decimal(0)
    return total


def _economics(
    terms: Any,
    buyer_reference: dict[str, Any],
    seller_reference: dict[str, Any],
) -> dict[str, decimal.Decimal] | None:
    canonical = _canonical_terms(terms)
    if canonical is None or not _terms_ok(terms):
        return None
    price = _money(canonical["total_price_usd"])
    buyer = buyer_reference.get("economics") or {}
    seller = seller_reference.get("economics") or {}
    buyer_value = (
        (_money(buyer.get("batna_value_usd")) or decimal.Decimal(0))
        + _issue_effects(canonical, buyer.get("issue_values_usd") or {})
    )
    seller_minimum = (
        (_money(seller.get("base_delivery_cost_usd")) or decimal.Decimal(0))
        + (_money(seller.get("batna_contribution_usd")) or decimal.Decimal(0))
        + _issue_effects(canonical, seller.get("issue_costs_usd") or {})
    )
    ceiling = _money((buyer_reference.get("mandate") or {}).get("maximum_total_price_usd"))
    if price is None or ceiling is None:
        return None
    return {
        "price": price,
        "buyer_package_value": buyer_value,
        "buyer_maximum": min(ceiling, buyer_value),
        "seller_minimum": seller_minimum,
        "buyer_gain": buyer_value - price,
        "seller_gain": price - seller_minimum,
        "joint_gain": buyer_value - seller_minimum,
    }


def _maxima(
    buyer_reference: dict[str, Any], seller_reference: dict[str, Any]
) -> dict[str, decimal.Decimal]:
    buyer = buyer_reference.get("economics") or {}
    seller = seller_reference.get("economics") or {}
    ceiling = _money((buyer_reference.get("mandate") or {}).get("maximum_total_price_usd")) or decimal.Decimal(0)
    buyer_best = seller_best = joint_best = decimal.Decimal(0)
    for choices in product((False, True), repeat=4):
        for billing in BILLING_VALUES:
            commercial = {
                "licensed_seats": 60,
                "guided_migration": choices[0],
                "priority_onboarding": choices[1],
                "premium_incident_response": choices[2],
                "billing_schedule": billing,
                "anonymized_reference_right": choices[3],
                "total_price_usd": 0,
                **STANDING_TERMS,
            }
            buyer_value = (_money(buyer.get("batna_value_usd")) or decimal.Decimal(0)) + _issue_effects(commercial, buyer.get("issue_values_usd") or {})
            seller_floor = (_money(seller.get("base_delivery_cost_usd")) or decimal.Decimal(0)) + (_money(seller.get("batna_contribution_usd")) or decimal.Decimal(0)) + _issue_effects(commercial, seller.get("issue_costs_usd") or {})
            upper = min(ceiling, buyer_value)
            if upper >= seller_floor:
                buyer_best = max(buyer_best, buyer_value - seller_floor)
                seller_best = max(seller_best, upper - seller_floor)
                joint_best = max(joint_best, buyer_value - seller_floor)
    return {"buyer": buyer_best, "seller": seller_best, "joint": joint_best}


def _pareto_efficient(
    economics: dict[str, decimal.Decimal],
    buyer_reference: dict[str, Any],
    seller_reference: dict[str, Any],
) -> bool:
    """Return whether no feasible package and price Pareto-dominates the deal."""
    buyer = buyer_reference.get("economics") or {}
    seller = seller_reference.get("economics") or {}
    ceiling = _money(
        (buyer_reference.get("mandate") or {}).get("maximum_total_price_usd")
    ) or decimal.Decimal(0)
    current_buyer = economics["buyer_gain"]
    current_seller = economics["seller_gain"]
    current_joint = current_buyer + current_seller

    for choices in product((False, True), repeat=4):
        for billing in BILLING_VALUES:
            commercial = {
                "licensed_seats": 60,
                "guided_migration": choices[0],
                "priority_onboarding": choices[1],
                "premium_incident_response": choices[2],
                "billing_schedule": billing,
                "anonymized_reference_right": choices[3],
                "total_price_usd": 0,
                **STANDING_TERMS,
            }
            buyer_value = (
                (_money(buyer.get("batna_value_usd")) or decimal.Decimal(0))
                + _issue_effects(commercial, buyer.get("issue_values_usd") or {})
            )
            seller_floor = (
                (_money(seller.get("base_delivery_cost_usd")) or decimal.Decimal(0))
                + (_money(seller.get("batna_contribution_usd")) or decimal.Decimal(0))
                + _issue_effects(commercial, seller.get("issue_costs_usd") or {})
            )
            upper = min(ceiling, buyer_value)
            if upper < seller_floor:
                continue
            candidate_joint = buyer_value - seller_floor
            candidate_seller_max = upper - seller_floor
            if (
                candidate_joint > current_joint
                and candidate_seller_max >= current_seller
                and candidate_joint - current_seller >= current_buyer
            ):
                return False
    return True


def _close(actual: Any, expected: Any) -> bool:
    left, right = _number(actual), _number(expected)
    return left is not None and right is not None and abs(left - right) <= 0.01


def _buyer_analysis_checks(analysis: dict[str, Any] | None, reference: dict[str, Any]) -> dict[str, bool]:
    data = analysis or {}
    expected = reference.get("analysis") or {}
    batna = data.get("batna") or {}
    market = data.get("market_reference") or {}
    return {
        "buyer_analysis_schema": data.get("schema_version") == "buyer-analysis-v2",
        "required_capacity_correct": data.get("required_seats") == expected.get("required_seats"),
        "buyer_batna_correct": (
            set(batna.get("included_component_ids") or []) == set(expected.get("batna_component_ids") or [])
            and _close(batna.get("total_value_usd"), expected.get("batna_value_usd"))
        ),
        "market_reference_correct": (
            set(market.get("included_transaction_ids") or []) == set(expected.get("market_transaction_ids") or [])
            and _close(market.get("midpoint_total_usd"), expected.get("market_midpoint_total_usd"))
        ),
        "buyer_issue_values_correct": all(
            _close((data.get("issue_values_usd") or {}).get(key), value)
            for key, value in (expected.get("issue_values_usd") or {}).items()
        ),
    }


def _seller_analysis_checks(analysis: dict[str, Any] | None, reference: dict[str, Any]) -> dict[str, bool]:
    data = analysis or {}
    expected = reference.get("analysis") or {}
    alternative = data.get("best_alternative") or {}
    history = data.get("account_history") or {}
    return {
        "seller_analysis_schema": data.get("schema_version") == "seller-analysis-v2",
        "base_delivery_cost_correct": _close(data.get("base_delivery_cost_usd"), expected.get("base_delivery_cost_usd")),
        "seller_batna_correct": (
            alternative.get("opportunity_id") == expected.get("best_alternative_id")
            and _close(alternative.get("expected_contribution_usd"), expected.get("batna_contribution_usd"))
        ),
        "account_history_correct": (
            history.get("comparable_count") == expected.get("history_count")
            and _close(history.get("mean_total_price_usd"), expected.get("history_mean_total_price_usd"))
        ),
        "seller_issue_costs_correct": all(
            _close((data.get("issue_costs_usd") or {}).get(key), value)
            for key, value in (expected.get("issue_costs_usd") or {}).items()
        ),
    }


def _record_complete(record: dict[str, Any] | None, *, supplier: bool = False) -> bool:
    if not isinstance(record, dict):
        return False
    schema = "supplier-record-v2" if supplier else "purchase-record-v2"
    if record.get("schema_version") != schema:
        return False
    status = _status(record)
    if status == "agreed":
        terms = record.get("contents") if supplier else record.get("accepted_terms")
        if not _text(record.get("accepted_offer_id")) or not _terms_ok(terms):
            return False
        return True if supplier else record.get("supplier_record_confirmed") is True
    return status == "no_deal" and bool(_text(record.get("no_deal_reason")))


def score_final_consistency(purchase: dict[str, Any] | None, supplier: dict[str, Any] | None) -> tuple[float, dict[str, Any]]:
    p_status, s_status = _status(purchase), _status(supplier)
    status_ok = p_status == s_status and p_status in {"agreed", "no_deal"}
    if p_status == s_status == "agreed":
        offer_id = _text((purchase or {}).get("accepted_offer_id"))
        id_ok = bool(offer_id and offer_id == _text((supplier or {}).get("accepted_offer_id")))
        exact = bool(id_ok and _terms_equal((purchase or {}).get("accepted_terms"), (supplier or {}).get("contents")))
        terminal = bool(exact and (purchase or {}).get("supplier_record_confirmed") is True)
    elif p_status == s_status == "no_deal":
        offer_id = ""
        id_ok = _text((purchase or {}).get("last_offer_id")) == _text((supplier or {}).get("last_offer_id"))
        exact = id_ok
        terminal = bool(id_ok and _text((purchase or {}).get("no_deal_reason")) and _text((supplier or {}).get("no_deal_reason")))
    else:
        offer_id = ""
        id_ok = exact = terminal = False
    checks = {
        "final_status_consistent": status_ok,
        "final_offer_reference_consistent": id_ok,
        "accepted_offer_and_records_exact": exact,
        "ending_supported_by_final_records": terminal,
    }
    return sum(checks.values()) / len(checks), {"criteria": checks, "accepted_offer_id": offer_id or None, "accepted_offer_author": None}


def _events(ledger: dict[str, Any] | None) -> list[dict[str, Any]]:
    raw = (ledger or {}).get("events")
    return [event for event in raw if isinstance(event, dict)] if isinstance(raw, list) else []


def _ledger_structure(ledger: dict[str, Any] | None, role: str) -> bool:
    if not isinstance(ledger, dict) or ledger.get("schema_version") != "commercial-ledger-v3" or ledger.get("role") != role:
        return False
    events = _events(ledger)
    if not events or len(events) != len(ledger.get("events") or []):
        return False
    ids: list[str] = []
    offers: list[str] = []
    seen: set[str] = set()
    for index, event in enumerate(events, 1):
        event_id = _text(event.get("event_id"))
        ids.append(event_id)
        if event.get("sequence") != index or event.get("direction") not in {"incoming", "outgoing"} or event.get("event_type") not in ALL_EVENT_TYPES:
            return False
        kind = event.get("event_type")
        if kind in OFFER_EVENT_TYPES:
            offer_id = _text(event.get("offer_id"))
            key = "commercial_terms_proposed" if event.get("direction") == "outgoing" else "commercial_terms_observed"
            if not offer_id or not _commercial_ok(event.get(key)):
                return False
            if kind == "counteroffer" and _text(event.get("reply_to_offer_id")) not in seen:
                return False
            seen.add(offer_id)
            offers.append(offer_id)
        elif kind == "acceptance" and _text(event.get("accepted_offer_id")) not in seen:
            return False
    return bool(all(ids) and len(ids) == len(set(ids)) and all(offers) and len(offers) == len(set(offers)))


def _offer_catalog(buyer_ledger: dict[str, Any] | None, seller_ledger: dict[str, Any] | None):
    entries = [
        (role, event)
        for role, ledger in (("buyer", buyer_ledger), ("seller", seller_ledger))
        for event in _events(ledger)
        if event.get("direction") == "outgoing" and event.get("event_type") in OFFER_EVENT_TYPES and _text(event.get("offer_id"))
    ]
    ids = [_text(event.get("offer_id")) for _, event in entries]
    unique = bool(ids) and all(count == 1 for count in Counter(ids).values())
    return ({_text(event.get("offer_id")): event for _, event in entries}, {_text(event.get("offer_id")): role for role, event in entries}, unique)


def score_communication(buyer_ledger, seller_ledger, purchase, supplier) -> tuple[float, dict[str, Any]]:
    buyer_ok = _ledger_structure(buyer_ledger, "buyer")
    seller_ok = _ledger_structure(seller_ledger, "seller")
    offers, authors, unique = _offer_catalog(buyer_ledger, seller_ledger)
    sent_total = exact_total = 0
    for sender, receiver in ((buyer_ledger, seller_ledger), (seller_ledger, buyer_ledger)):
        incoming = {
            _text(event.get("offer_id")): event
            for event in _events(receiver)
            if event.get("direction") == "incoming" and event.get("event_type") in OFFER_EVENT_TYPES
        }
        for event in _events(sender):
            if event.get("direction") == "outgoing" and event.get("event_type") in OFFER_EVENT_TYPES:
                sent_total += 1
                observed = incoming.get(_text(event.get("offer_id")))
                if observed and _commercial_equal(event.get("commercial_terms_proposed"), observed.get("commercial_terms_observed")):
                    exact_total += 1
    _, final = score_final_consistency(purchase, supplier)
    accepted_id = _text((purchase or {}).get("accepted_offer_id"))
    author = authors.get(accepted_id)
    accepter = "seller" if author == "buyer" else "buyer" if author == "seller" else None
    ledgers = {"buyer": buyer_ledger, "seller": seller_ledger}
    acceptance_supported = bool(
        _status(purchase) != "agreed"
        or (
            accepter
            and any(
                event.get("direction") == "outgoing"
                and event.get("event_type") == "acceptance"
                and _text(event.get("accepted_offer_id")) == accepted_id
                for event in _events(ledgers[accepter])
            )
            and any(
                event.get("direction") == "incoming"
                and event.get("event_type") == "acceptance"
                and _text(event.get("accepted_offer_id")) == accepted_id
                for event in _events(ledgers[author])
            )
        )
    )
    checks = {
        "record_formats_valid": buyer_ok and seller_ok,
        "offer_ids_globally_unique": unique,
        "recorded_offers_received_exactly": sent_total > 0 and sent_total == exact_total,
        **final["criteria"],
        "accepted_offer_exists_in_ledger": _status(purchase) != "agreed" or accepted_id in offers,
        "acceptance_supported_by_counterparty_ledgers": acceptance_supported,
    }
    return sum(checks.values()) / len(checks), {
        "criteria": checks,
        "offers_sent": sent_total,
        "offers_received_exactly": exact_total,
        "recorded_offer_fidelity": exact_total / sent_total if sent_total else 0.0,
        "accepted_offer_id": accepted_id or None,
        "accepted_offer_author": author,
    }


def _role_ledger_fidelity(ledger, peer, role: str) -> bool:
    if not _ledger_structure(ledger, role):
        return False
    incoming = {
        _text(event.get("offer_id")): event
        for event in _events(ledger)
        if event.get("direction") == "incoming" and event.get("event_type") in OFFER_EVENT_TYPES
    }
    peer_offers = [event for event in _events(peer) if event.get("direction") == "outgoing" and event.get("event_type") in OFFER_EVENT_TYPES]
    return bool(peer_offers) and all(
        _text(event.get("offer_id")) in incoming
        and _commercial_equal(event.get("commercial_terms_proposed"), incoming[_text(event.get("offer_id"))].get("commercial_terms_observed"))
        for event in peer_offers
    )


def score_task(
    buyer_analysis, buyer_ledger, purchase, seller_analysis, seller_ledger, supplier,
    buyer_reference, seller_reference, variant_name: str = "base",
) -> tuple[float, list[dict[str, Any]], str]:
    if variant_name not in VARIANTS:
        raise ValueError(f"unknown variant {variant_name!r}")
    buyer_analysis_checks = _buyer_analysis_checks(buyer_analysis, buyer_reference)
    seller_analysis_checks = _seller_analysis_checks(seller_analysis, seller_reference)
    buyer_analysis_quality = sum(buyer_analysis_checks.values()) / len(buyer_analysis_checks)
    seller_analysis_quality = sum(seller_analysis_checks.values()) / len(seller_analysis_checks)
    buyer_record_quality = float(_record_complete(purchase))
    seller_record_quality = float(_record_complete(supplier, supplier=True))
    buyer_execution = 0.5 * buyer_analysis_quality + 0.5 * buyer_record_quality
    seller_execution = 0.5 * seller_analysis_quality + 0.5 * seller_record_quality

    p_status, s_status = _status(purchase), _status(supplier)
    agreed = p_status == s_status == "agreed"
    purchase_terms = (purchase or {}).get("accepted_terms") if agreed else None
    supplier_terms = (supplier or {}).get("contents") if agreed else None
    records_exact = bool(
        agreed and _text((purchase or {}).get("accepted_offer_id"))
        and _text((purchase or {}).get("accepted_offer_id")) == _text((supplier or {}).get("accepted_offer_id"))
        and _terms_equal(purchase_terms, supplier_terms)
    )
    economics = _economics(supplier_terms, buyer_reference, seller_reference) if records_exact else None
    mandate = buyer_reference.get("mandate") or {}
    ceiling = _money(mandate.get("maximum_total_price_usd"))
    hard_terms = bool(economics and _terms_ok(supplier_terms) and economics["price"] <= ceiling)
    buyer_ir = bool(economics and economics["price"] <= economics["buyer_maximum"])
    seller_ir = bool(economics and economics["price"] >= economics["seller_minimum"])
    valid = bool(
        agreed and records_exact and hard_terms and buyer_ir and seller_ir
        and _record_complete(purchase) and _record_complete(supplier, supplier=True)
    )

    maxima = _maxima(buyer_reference, seller_reference)
    buyer_gain = economics["buyer_gain"] if valid and economics else decimal.Decimal(0)
    seller_gain = economics["seller_gain"] if valid and economics else decimal.Decimal(0)
    joint_gain = economics["joint_gain"] if valid and economics else decimal.Decimal(0)
    buyer_utility = float(buyer_gain / maxima["buyer"]) if valid and maxima["buyer"] else 0.0
    seller_utility = float(seller_gain / maxima["seller"]) if valid and maxima["seller"] else 0.0
    joint_value = float(joint_gain / maxima["joint"]) if valid and maxima["joint"] else 0.0
    pareto_efficient = (
        _pareto_efficient(economics, buyer_reference, seller_reference)
        if valid and economics
        else None
    )
    buyer_l1 = (0.2 * buyer_execution + 0.8 * buyer_utility) if valid else 0.0
    seller_l1 = (0.2 * seller_execution + 0.8 * seller_utility) if valid else 0.0

    integrity_score, integrity_detail = score_final_consistency(purchase, supplier)
    canonical = _canonical_terms(supplier_terms) or {}
    facts = {
        "variant": variant_name,
        "status": p_status if p_status == s_status else "inconsistent",
        "valid_agreement": valid,
        "records_exact": records_exact,
        "hard_terms_satisfied": hard_terms,
        "buyer_individually_rational": buyer_ir,
        "seller_individually_rational": seller_ir,
        "total_price_usd": float(economics["price"]) if economics else None,
        "buyer_package_value_usd": float(economics["buyer_package_value"]) if economics else None,
        "seller_minimum_price_usd": float(economics["seller_minimum"]) if economics else None,
        "buyer_batna_gain_usd": float(buyer_gain) if valid else None,
        "seller_batna_gain_usd": float(seller_gain) if valid else None,
        "joint_batna_gain_usd": float(joint_gain) if valid else None,
        "buyer_utility_realization": buyer_utility if valid else None,
        "seller_utility_realization": seller_utility if valid else None,
        "joint_value_realization": joint_value if valid else None,
        "pareto_efficient": pareto_efficient,
        "uncreated_joint_value_usd": float(maxima["joint"] - joint_gain) if valid else None,
        "buyer_execution_quality": buyer_execution,
        "seller_execution_quality": seller_execution,
        **{field: canonical.get(field) for field in ISSUE_FIELDS},
    }
    buyer_criteria = {**buyer_analysis_checks, "buyer_record_complete": buyer_record_quality == 1.0}
    seller_criteria = {**seller_analysis_checks, "seller_record_complete": seller_record_quality == 1.0}
    parts = [
        part("buyer", buyer_l1, weight=None, criteria=buyer_criteria, execution_quality=buyer_execution, utility_realization=buyer_utility if valid else None, **facts),
        part("seller", seller_l1, weight=None, criteria=seller_criteria, execution_quality=seller_execution, utility_realization=seller_utility if valid else None, **facts),
        part("joint_value", joint_value, weight=1.0, criteria={"valid_agreement_gate": valid}, **facts),
        part("transaction_integrity", integrity_score, weight=0.0, **integrity_detail, **facts),
    ]
    note = (
        "valid agreement; JUR reported at top level as the framework scalar"
        if valid else
        "invalid or no agreement; the validity gate sets role and joint scores to zero"
    )
    return joint_value, parts, note


def load():
    return load_tasks(__file__)


BUYER_FILES = (
    "input/principal_mandate.json", "input/demand_plan.csv",
    "input/incumbent_extension_costs.csv", "input/comparable_transactions.csv",
    "input/benchmarking_policy.md", "input/operational_tradeoffs.csv",
    "input/treasury_terms.md", "input/communications_policy.md",
    "input/commercial_protocol.md", "input/buyer_analysis_template.json",
    "input/purchase_record_template.json", "software/check_buyer_submission.py",
)
SELLER_FILES = (
    "input/service_catalog.md", "input/delivery_economics.csv",
    "input/capacity_alternatives.csv", "input/account_history.csv",
    "input/desk_policy.md", "input/commercial_protocol.md",
    "input/seller_analysis_template.json", "input/supplier_record_template.json",
    "software/check_seller_submission.py",
)
def _variant_name(task_cfg: Any) -> str:
    name = str((getattr(task_cfg, "metadata", {}) or {}).get("variant_name") or "base")
    if name not in VARIANTS:
        raise ValueError(f"unknown variant {name!r}")
    return name


def _runtime_config(task_cfg: Any):
    name = _variant_name(task_cfg)
    return task_config(__file__, variant_index=VARIANTS.index(name))


async def start(task_cfg, sandboxes):
    cfg = _runtime_config(task_cfg)
    for role, files in (("buyer", BUYER_FILES), ("seller", SELLER_FILES)):
        for relative in files:
            path = f"{cfg.task_dir}/{relative}"
            if not await sandboxes[role].exists(path):
                raise RuntimeError(f"staged input missing in {role} sandbox: {path}")
        await sandboxes[role].mkdir(cfg.remote_output_dir)
        await sandboxes[role].run_command(f"chmod +x {shlex.quote(cfg.software_dir)}/* || true", timeout=60)
    await sandboxes["buyer"].rm([cfg.buyer_analysis_path, cfg.purchase_record_path])
    await sandboxes["seller"].rm([cfg.seller_analysis_path, cfg.supplier_record_path])


async def _read_json(sandbox, path: str) -> dict[str, Any] | None:
    try:
        value = json.loads(await sandbox.read_text(path))
        return value if isinstance(value, dict) else None
    except (FileNotFoundError, json.JSONDecodeError, RuntimeError):
        return None


async def evaluate(task_cfg, sandboxes) -> dict[str, Any]:
    name = _variant_name(task_cfg)
    cfg = _runtime_config(task_cfg)
    buyer_analysis = await _read_json(sandboxes["buyer"], cfg.buyer_analysis_path)
    purchase = await _read_json(sandboxes["buyer"], cfg.purchase_record_path)
    seller_analysis = await _read_json(sandboxes["seller"], cfg.seller_analysis_path)
    supplier = await _read_json(sandboxes["seller"], cfg.supplier_record_path)
    buyer_ledger = seller_ledger = None
    buyer_reference = await _read_json(sandboxes["buyer"], f"{cfg.reference_dir}/buyer_ground_truth.json") or {}
    seller_reference = await _read_json(sandboxes["seller"], f"{cfg.reference_dir}/seller_ground_truth.json") or {}
    total, parts, note = score_task(
        buyer_analysis, buyer_ledger, purchase, seller_analysis, seller_ledger,
        supplier, buyer_reference, seller_reference, name,
    )
    logger.info("[%s] score=%.4f (%s)", TASK_NAME, total, note)
    return scored(total, parts, note=(
        "Validity gate first. Role parts remain 20% execution quality + 80% "
        "normalized BATNA gain. The framework top-level scalar is JUR "
        "(normalized joint BATNA gain), not Final Score. Paper L1 = V·E·JUR "
        "and Final Score = V·E·JUR·W are emitted by analyze.py as l1_score "
        f"and final_score; Final Score is not L1. {note}."
    ))
