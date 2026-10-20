"""Offline process, outcome, resource, and cost metrics for this negotiation."""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import re
import sys
from decimal import Decimal, InvalidOperation
from itertools import pairwise, product
from pathlib import Path
from typing import Any

from a2_run.transcript.loader import RunView, iter_runs
from metrics.base import Metric, MetricReport, RoleScore, run_metrics, write_metrics

TASK_SUFFIX = "procurement/interest_seeking_negotiation"
OFFER_TYPES = {"offer", "counteroffer"}
TERM_FIELDS = (
    "licensed_seats",
    "guided_migration",
    "priority_onboarding",
    "premium_incident_response",
    "billing_schedule",
    "anonymized_reference_right",
    "total_price_usd",
)
NEGOTIABLE_ISSUE_FIELDS = tuple(
    field
    for field in TERM_FIELDS
    if field not in {"licensed_seats", "total_price_usd"}
)
TRANSCRIPT_TERM_FIELDS = tuple(
    field for field in TERM_FIELDS if field != "licensed_seats"
)
MONEY_FIELDS = {"total_price_usd"}
ACCEPTANCE_RE = re.compile(
    r"\b(?:accept(?:s|ed|ance|ing)?|agree(?:s|d|ment)?)\b",
    re.IGNORECASE,
)
WITHDRAWAL_RE = re.compile(
    r"\bwithdraw(?:s|n|al|ing)?(?:\s+(?:formal\s+)?offer)?\s+"
    r"([A-Za-z0-9][A-Za-z0-9._-]*)\b",
    re.IGNORECASE,
)
FORMAL_OFFER_RE = re.compile(
    r"^\s*(?:[-*]\s*)?"
    r"(?:(?:(?:buyer|seller|supplier)\s+)|"
    r"(?:[A-Za-z][^\n:]{0,72}\b(?:submits?|issues?|presents?)\s+))?"
    r"(?:a\s+)?(?:formal\s+)?(?:counteroffer|offer)\s+"
    r"(?:id\s*[:#-]?\s*)?"
    r"([A-Za-z0-9][A-Za-z0-9._-]*)\b",
    re.IGNORECASE | re.MULTILINE,
)

# Frozen research-side pricing assumptions.  These values are deliberately
# kept out of agent-visible task data and out of the deterministic task score.
# ``input_tokens`` includes cached/cache-write input for OpenAI Responses, so
# those categories are removed before the standard-input rate is applied.
TOKEN_PRICING_USD_PER_MILLION = {
    "gpt-5.6-sol": {
        "input": Decimal("4.00"),
        "cache_read": Decimal("0.40"),
        "cache_creation": Decimal("5.00"),
        "output": Decimal("20.00"),
    },
    "gpt-5.6-terra": {
        "input": Decimal("2.00"),
        "cache_read": Decimal("0.20"),
        "cache_creation": Decimal("2.50"),
        "output": Decimal("12.00"),
    },
    "gpt-5.6-luna": {
        "input": Decimal("0.20"),
        "cache_read": Decimal("0.02"),
        "cache_creation": Decimal("0.25"),
        "output": Decimal("1.20"),
    },
}
TOKEN_PRICING_EFFECTIVE_DATE = "2026-09-15"
TOKEN_PRICING_SOURCE = (
    "https://developers.openai.com/api/docs/models/gpt-5.6-sol; "
    "https://developers.openai.com/api/docs/models/gpt-5.6-terra; "
    "https://developers.openai.com/api/docs/models/gpt-5.6-luna"
)

BUYER_BATNA_USD = Decimal(20400)
BUYER_CEILING_USD = Decimal(23000)
SELLER_BASE_AND_BATNA_USD = Decimal(12000)
BUYER_ISSUE_VALUES = {
    "guided_migration": Decimal(4000),
    "priority_onboarding": Decimal(2500),
    "premium_incident_response": Decimal(1800),
    "quarterly_billing": Decimal(900),
    "anonymized_reference_right": Decimal(-700),
}
SELLER_ISSUE_COSTS = {
    "guided_migration": Decimal(1200),
    "priority_onboarding": Decimal(1800),
    "premium_incident_response": Decimal(600),
    "quarterly_billing": Decimal(400),
    "anonymized_reference_right": Decimal(-2200),
}
ROLE_REFERENCE_USD = {"buyer": Decimal(17550), "seller": Decimal(12500)}
BUYER_UTILITY_MAX_USD = Decimal(15100)
SELLER_UTILITY_MAX_USD = Decimal(12000)
DEFAULT_BEHAVIOR_FLOOR = Decimal("0.25")


def _score_value(value: float | Decimal | None) -> Decimal | None:
    """Convert a present 0-1 score to Decimal while preserving N/A as None."""
    if value is None:
        return None
    score = Decimal(str(value))
    if not Decimal(0) <= score <= Decimal(1):
        raise ValueError(f"score must be in [0, 1], got {value!r}")
    return score


def _mean_available(*values: float | Decimal | None) -> float | None:
    """Average present values and exclude N/A rather than coercing it to zero."""
    available = [score for value in values if (score := _score_value(value)) is not None]
    if not available:
        return None
    return float(sum(available, Decimal(0)) / len(available))


def _principal_balance(
    buyer_utility_realization: float | Decimal,
    seller_utility_realization: float | Decimal,
) -> float:
    """Return a 0-1 balance term separated from the amount of joint utility."""
    buyer = _score_value(buyer_utility_realization)
    seller = _score_value(seller_utility_realization)
    assert buyer is not None and seller is not None
    buyer_gain = buyer * BUYER_UTILITY_MAX_USD
    seller_gain = seller * SELLER_UTILITY_MAX_USD
    total = buyer_gain + seller_gain
    if total == 0:
        return 0.0
    return float(2 * (buyer_gain * seller_gain).sqrt() / total)


def _role_behavior_score(
    dpe: float | Decimal | None,
    fpd: float | Decimal | None,
    sr: float | Decimal | None,
) -> float | None:
    """Average the three applicable behavior scores for one role."""
    return _mean_available(dpe, fpd, sr)


def _pair_behavior_score(
    buyer_behavior: float | Decimal | None,
    seller_behavior: float | Decimal | None,
) -> float | None:
    """Give Buyer and Seller equal weight after each role is scored."""
    return _mean_available(buyer_behavior, seller_behavior)


def _behavior_multiplier(
    pair_behavior: float | Decimal | None,
    floor: float | Decimal = DEFAULT_BEHAVIOR_FLOOR,
) -> float:
    """Return a neutral multiplier for N/A, otherwise floor + (1-floor)*score."""
    behavior = _score_value(pair_behavior)
    floor_score = _score_value(floor)
    assert floor_score is not None
    if behavior is None:
        return 1.0
    return float(floor_score + (Decimal(1) - floor_score) * behavior)


def _l1_score(
    *,
    valid: bool,
    buyer_execution: float | Decimal,
    seller_execution: float | Decimal,
    joint_utility_realization: float | Decimal,
) -> float:
    """Paper L1: validity × execution × JUR. Does not include behavior (W)."""
    if not valid:
        return 0.0
    execution = _mean_available(buyer_execution, seller_execution)
    joint = _score_value(joint_utility_realization)
    assert execution is not None and joint is not None
    return float(Decimal(str(execution)) * joint)


def _candidate_overall_score(
    *,
    valid: bool,
    buyer_execution: float | Decimal,
    seller_execution: float | Decimal,
    joint_utility_realization: float | Decimal,
    buyer_behavior: float | Decimal | None,
    seller_behavior: float | Decimal | None,
    behavior_floor: float | Decimal = DEFAULT_BEHAVIOR_FLOOR,
) -> float:
    """Final / composite score: L1 × behavior (W). This is not paper L1."""
    if not valid:
        return 0.0
    l1 = Decimal(str(_l1_score(
        valid=True,
        buyer_execution=buyer_execution,
        seller_execution=seller_execution,
        joint_utility_realization=joint_utility_realization,
    )))
    pair_behavior = _pair_behavior_score(buyer_behavior, seller_behavior)
    behavior = _behavior_multiplier(pair_behavior, behavior_floor)
    return float(l1 * Decimal(str(behavior)))


def _feasible_package_bounds() -> list[tuple[Decimal, Decimal, Decimal]]:
    """Return ``(buyer value, seller floor, maximum price)`` for every package."""
    rows: list[tuple[Decimal, Decimal, Decimal]] = []
    issue_names = tuple(BUYER_ISSUE_VALUES)
    for enabled in product((False, True), repeat=len(issue_names)):
        buyer_value = BUYER_BATNA_USD + sum(
            (
                BUYER_ISSUE_VALUES[name]
                for name, selected in zip(issue_names, enabled, strict=True)
                if selected
            ),
            Decimal(0),
        )
        seller_floor = SELLER_BASE_AND_BATNA_USD + sum(
            (
                SELLER_ISSUE_COSTS[name]
                for name, selected in zip(issue_names, enabled, strict=True)
                if selected
            ),
            Decimal(0),
        )
        maximum_price = min(buyer_value, BUYER_CEILING_USD)
        if seller_floor <= maximum_price:
            rows.append((buyer_value, seller_floor, maximum_price))
    return rows


def _safe_improvement_headroom(
    first_economics: dict[str, Decimal],
    role: str,
) -> Decimal:
    """Compute the greatest Pareto-safe utility gain available to one role."""
    if role not in {"buyer", "seller"}:
        raise ValueError(f"unknown role: {role}")
    buyer_start = first_economics["buyer_gain"]
    seller_start = first_economics["seller_gain"]
    best = Decimal(0)
    for buyer_value, seller_floor, maximum_price in _feasible_package_bounds():
        if role == "buyer":
            price = seller_floor + seller_start
            if price <= maximum_price:
                best = max(best, buyer_value - price - buyer_start)
        else:
            price = min(maximum_price, buyer_value - buyer_start)
            if price >= seller_floor:
                best = max(best, price - seller_floor - seller_start)
    return max(best, Decimal(0))


def _withdrawn_offer_ids(run: RunView, through_turn: int) -> set[str]:
    """Return offer IDs explicitly withdrawn by the acceptance turn."""
    withdrawn: set[str] = set()
    for position, turn in enumerate(run.turns, 1):
        if position > through_turn:
            break
        withdrawn.update(
            match.group(1).upper()
            for match in WITHDRAWAL_RE.finditer(turn.reply)
        )
    return withdrawn


def _agentic_contribution_metrics(
    run: RunView,
    protocol_offers: list[dict[str, Any]],
    first_feasible: dict[str, Any] | None,
    *,
    accepted_offer_id: str,
    accepted_offer_author: str | None,
    acceptance_turn: int | None,
) -> dict[str, int | float | None]:
    """Score value-directed action after the first feasible formal offer.

    All fields are derived from protocol-form offers and scenario ground truth.
    Missing evaluation opportunities remain ``None`` and are excluded from the
    role and pair means.
    """
    result: dict[str, int | float | None] = {}
    for role in ("buyer", "seller"):
        for metric in ("vco", "dpe", "fpd", "sr", "behavior"):
            result[f"{role}_{metric}"] = None
        result[f"{role}_safe_improvement_headroom_usd"] = None
        result[f"{role}_behavior_applicable_count"] = 0
    result.update(
        {
            "pair_behavior": None,
            "behavior_multiplier": 1.0,
            "behavior_floor": float(DEFAULT_BEHAVIOR_FLOOR),
        }
    )
    if not first_feasible or not first_feasible.get("economics"):
        return result

    first_turn = int(first_feasible["turn"])
    first_economics = first_feasible["economics"]
    negotiation_end = acceptance_turn or len(run.turns)
    later_offers = [
        row
        for row in protocol_offers
        if first_turn < int(row["turn"]) <= negotiation_end and row.get("economics")
    ]
    role_maximum = {
        "buyer": BUYER_UTILITY_MAX_USD,
        "seller": SELLER_UTILITY_MAX_USD,
    }

    for role in ("buyer", "seller"):
        other = "seller" if role == "buyer" else "buyer"
        own_key = f"{role}_gain"
        other_key = f"{other}_gain"
        own_start = first_economics[own_key]
        other_start = first_economics[other_key]
        action_opportunity = any(
            turn.role == role
            for position, turn in enumerate(run.turns, 1)
            if first_turn < position <= negotiation_end
        )
        authored = [row for row in later_offers if row["role"] == role]
        headroom = _safe_improvement_headroom(first_economics, role)
        result[f"{role}_vco"] = int(headroom > 0)
        result[f"{role}_safe_improvement_headroom_usd"] = float(headroom)

        if action_opportunity and own_start < role_maximum[role]:
            result[f"{role}_dpe"] = int(
                any(row["economics"][own_key] > own_start for row in authored)
            )

        if action_opportunity and headroom > 0:
            safe_gains = [
                row["economics"][own_key] - own_start
                for row in authored
                if row["economics"]["feasible"] == 1
                and row["economics"][own_key] > own_start
                and row["economics"][other_key] >= other_start
            ]
            realized = max(safe_gains, default=Decimal(0))
            result[f"{role}_fpd"] = float(min(realized / headroom, Decimal(1)))

    accepted_key = accepted_offer_id.upper()
    accepted_row = next(
        (row for row in protocol_offers if row["offer_id"] == accepted_key),
        None,
    )
    if accepted_row and accepted_offer_author not in {"buyer", "seller"}:
        accepted_offer_author = accepted_row["role"]
    if (
        accepted_row
        and accepted_row.get("economics")
        and accepted_offer_author in {"buyer", "seller"}
        and acceptance_turn is not None
    ):
        accepter = "seller" if accepted_offer_author == "buyer" else "buyer"
        withdrawn = _withdrawn_offer_ids(run, acceptance_turn)
        open_counterpart_offers = [
            row
            for row in protocol_offers
            if row["role"] == accepted_offer_author
            and int(row["turn"]) < acceptance_turn
            and row["offer_id"] not in withdrawn
            and row.get("economics")
            and row["economics"]["feasible"] == 1
        ]
        utilities = [
            row["economics"][f"{accepter}_gain"]
            for row in open_counterpart_offers
        ]
        if len(set(utilities)) > 1 and max(utilities) > 0:
            retained = accepted_row["economics"][f"{accepter}_gain"] / max(utilities)
            result[f"{accepter}_sr"] = float(
                min(max(retained, Decimal(0)), Decimal(1))
            )

    for role in ("buyer", "seller"):
        behavior_values = [
            result[f"{role}_dpe"],
            result[f"{role}_fpd"],
            result[f"{role}_sr"],
        ]
        result[f"{role}_behavior_applicable_count"] = sum(
            value is not None for value in behavior_values
        )
        result[f"{role}_behavior"] = _role_behavior_score(*behavior_values)
    result["pair_behavior"] = _pair_behavior_score(
        result["buyer_behavior"],
        result["seller_behavior"],
    )
    result["behavior_multiplier"] = _behavior_multiplier(result["pair_behavior"])
    return result


def _is_this_task(run: RunView) -> bool:
    value = (run.task_path or run.task_slug).replace("__", "/").strip("/")
    return value.endswith(TASK_SUFFIX)


def _facts(run: RunView) -> dict[str, Any]:
    parts = (run.eval.get("breakdown") or {}).get("parts") or []
    # Every scorer part intentionally receives the same research facts.  The
    # transaction part is preferred because it also carries communication facts.
    for wanted in ("transaction_integrity", "buyer", "seller"):
        for item in parts:
            if isinstance(item, dict) and item.get("role") == wanted:
                facts = item.get("facts")
                if isinstance(facts, dict):
                    return facts
    return {}


def _read_ledger(run: RunView, role: str) -> dict[str, Any] | None:
    root = run.run_dir / "output" / role
    direct = root / "negotiation_ledger.json"
    candidates = [direct] if direct.is_file() else sorted(root.rglob("negotiation_ledger.json"))
    if len(candidates) != 1:
        return None
    try:
        value = json.loads(candidates[0].read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _events(ledger: dict[str, Any] | None) -> list[dict[str, Any]]:
    raw = (ledger or {}).get("events")
    return [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _number_present(text: str, value: Any) -> bool:
    number = _decimal(value)
    if number is None:
        return False
    compact = text.replace(",", "")
    plain = format(number, "f")
    if "." in plain:
        plain = plain.rstrip("0").rstrip(".")
    if not plain:
        plain = "0"
    if re.search(rf"(?<![\d.]){re.escape(plain)}(?:\.0+)?(?!\d|\.\d)", compact):
        return True
    if number != 0 and number % 1000 == 0:
        thousands = format(number / 1000, "f")
        if "." in thousands:
            thousands = thousands.rstrip("0").rstrip(".")
        return bool(re.search(rf"(?<![\d.]){re.escape(thousands)}\s*k\b", compact, re.IGNORECASE))
    return False


def _tokens_present(text: str, value: Any) -> bool:
    tokens = [t for t in re.split(r"[_\s]+", str(value).lower()) if t]
    lowered = text.lower().replace("_", " ")
    return bool(tokens) and all(token in lowered for token in tokens)


def _term_present(text: str, field: str, value: Any) -> bool:
    if field in MONEY_FIELDS or field == "licensed_seats":
        return _number_present(text, value)
    return _tokens_present(text, value)


def _outgoing_offer_events(ledger: dict[str, Any] | None) -> list[dict[str, Any]]:
    return [
        event
        for event in _events(ledger)
        if event.get("direction") == "outgoing"
        and event.get("event_type") in OFFER_TYPES
        and str(event.get("offer_id") or "").strip()
    ]


def _matching_offer_turns(
    run: RunView,
    role: str,
    event: dict[str, Any],
) -> list[tuple[int, int]]:
    """Return ``(turn position, present-term count)`` for messages citing an offer."""
    offer_id = str(event.get("offer_id") or "").strip()
    terms = event.get("commercial_terms_proposed") or {}
    if not offer_id or not isinstance(terms, dict):
        return []
    pattern = re.compile(rf"\b{re.escape(offer_id)}\b", re.IGNORECASE)
    found: list[tuple[int, int]] = []
    for position, turn in enumerate(run.turns, 1):
        if turn.role != role or not pattern.search(turn.reply):
            continue
        present = sum(
            field in terms and _term_present(turn.reply, field, terms[field])
            for field in TERM_FIELDS
        )
        # The author may cite its old offer while acknowledging acceptance.
        # Treat a turn as the offer utterance only when it carries actual terms.
        if present:
            found.append((position, present))
    return found


def _acceptance_turn(
    run: RunView,
    accepted_offer_id: str,
    offer_author: str | None,
) -> int | None:
    if not accepted_offer_id or offer_author not in {"buyer", "seller"}:
        return None
    accepter = "seller" if offer_author == "buyer" else "buyer"
    id_re = re.compile(rf"\b{re.escape(accepted_offer_id)}\b", re.IGNORECASE)
    for position, turn in enumerate(run.turns, 1):
        if turn.role == accepter and id_re.search(turn.reply) and ACCEPTANCE_RE.search(turn.reply):
            return position
    return None


def _transcript_offers(run: RunView) -> list[tuple[str, str, int]]:
    """Extract each role's first introduction of a protocol-form offer ID."""
    rows: list[tuple[str, str, int]] = []
    seen: set[tuple[str, str]] = set()
    for position, turn in enumerate(run.turns, 1):
        for match in FORMAL_OFFER_RE.finditer(turn.reply):
            key = (turn.role, match.group(1).upper())
            if key not in seen:
                seen.add(key)
                rows.append((turn.role, key[1], position))
    return rows


def _transcript_offer_terms(run: RunView) -> list[dict[str, Any]]:
    """Parse protocol-form JSON offers from delivered messages."""
    rows: list[dict[str, Any]] = []
    decoder = json.JSONDecoder()
    seen: set[tuple[str, str]] = set()
    for position, turn in enumerate(run.turns, 1):
        matches = list(FORMAL_OFFER_RE.finditer(turn.reply))
        for index, match in enumerate(matches):
            offer_id = match.group(1).upper()
            key = (turn.role, offer_id)
            if key in seen:
                continue
            limit = matches[index + 1].start() if index + 1 < len(matches) else len(turn.reply)
            start = turn.reply.find("{", match.end(), limit)
            if start < 0:
                continue
            try:
                terms, _ = decoder.raw_decode(turn.reply[start:limit])
            except (TypeError, ValueError):
                continue
            # The written protocol makes 60 seats a standing inherited term, so
            # transcript JSON contains only the six variable commercial fields.
            if not isinstance(terms, dict) or not all(
                field in terms for field in TRANSCRIPT_TERM_FIELDS
            ):
                continue
            seen.add(key)
            rows.append({"role": turn.role, "offer_id": offer_id, "turn": position, "terms": terms})
    return rows


def _offer_economics(terms: dict[str, Any]) -> dict[str, Decimal] | None:
    price = _decimal(terms.get("total_price_usd"))
    if price is None:
        return None
    buyer_value = BUYER_BATNA_USD
    seller_floor = SELLER_BASE_AND_BATNA_USD
    for issue in (
        "guided_migration", "priority_onboarding",
        "premium_incident_response", "anonymized_reference_right",
    ):
        if terms.get(issue) is True:
            buyer_value += BUYER_ISSUE_VALUES[issue]
            seller_floor += SELLER_ISSUE_COSTS[issue]
    if terms.get("billing_schedule") == "quarterly_net_30":
        buyer_value += BUYER_ISSUE_VALUES["quarterly_billing"]
        seller_floor += SELLER_ISSUE_COSTS["quarterly_billing"]
    return {
        "price": price,
        "buyer_value": buyer_value,
        "seller_floor": seller_floor,
        "buyer_gain": buyer_value - price,
        "seller_gain": price - seller_floor,
        "joint_gain": buyer_value - seller_floor,
        "feasible": Decimal(int(price <= min(BUYER_CEILING_USD, buyer_value) and price >= seller_floor)),
    }


def _offer_pareto_efficient(economics: dict[str, Decimal] | None) -> bool | None:
    """Evaluate a transcript offer against all feasible packages and prices."""
    if economics is None:
        return None
    if economics["feasible"] != 1:
        return False
    current_buyer = economics["buyer_gain"]
    current_seller = economics["seller_gain"]
    current_joint = economics["joint_gain"]
    issues = (
        "guided_migration",
        "priority_onboarding",
        "premium_incident_response",
        "anonymized_reference_right",
    )
    for choices in product((False, True), repeat=len(issues)):
        for quarterly in (False, True):
            buyer_value = BUYER_BATNA_USD
            seller_floor = SELLER_BASE_AND_BATNA_USD
            for issue, selected in zip(issues, choices, strict=True):
                if selected:
                    buyer_value += BUYER_ISSUE_VALUES[issue]
                    seller_floor += SELLER_ISSUE_COSTS[issue]
            if quarterly:
                buyer_value += BUYER_ISSUE_VALUES["quarterly_billing"]
                seller_floor += SELLER_ISSUE_COSTS["quarterly_billing"]
            upper = min(BUYER_CEILING_USD, buyer_value)
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


def _offer_change_metrics(
    first_economics: dict[str, Decimal] | None,
    facts: dict[str, Any],
) -> dict[str, int | float | None]:
    """Return first-offer quality and first-to-final utility changes."""
    pareto = _offer_pareto_efficient(first_economics)
    result: dict[str, int | float | None] = {
        "first_offer_feasible": (
            int(first_economics["feasible"]) if first_economics else None
        ),
        "first_offer_pareto_efficient": (
            int(pareto) if isinstance(pareto, bool) else None
        ),
        "first_offer_buyer_utility_usd": (
            float(first_economics["buyer_gain"]) if first_economics else None
        ),
        "first_offer_seller_utility_usd": (
            float(first_economics["seller_gain"]) if first_economics else None
        ),
        "first_offer_joint_utility_usd": (
            float(first_economics["joint_gain"]) if first_economics else None
        ),
        "buyer_utility_delta_first_to_final_usd": None,
        "seller_utility_delta_first_to_final_usd": None,
        "joint_utility_delta_first_to_final_usd": None,
    }
    if not first_economics or not facts.get("valid_agreement"):
        return result
    final_values = {
        "buyer": _decimal(facts.get("buyer_batna_gain_usd")),
        "seller": _decimal(facts.get("seller_batna_gain_usd")),
        "joint": _decimal(facts.get("joint_batna_gain_usd")),
    }
    for role in ("buyer", "seller", "joint"):
        final = final_values[role]
        if final is not None:
            result[f"{role}_utility_delta_first_to_final_usd"] = float(
                final - first_economics[f"{role}_gain"]
            )
    return result


def _offer_path_quality_metrics(
    protocol_offers: list[dict[str, Any]],
    facts: dict[str, Any],
) -> dict[str, int | float | None]:
    """Measure whether the final agreement preserves the best feasible offer seen."""
    feasible = [
        row
        for row in protocol_offers
        if row.get("economics") and row["economics"]["feasible"] == 1
    ]
    result: dict[str, int | float | None] = {
        "peak_feasible_buyer_utility_usd": None,
        "peak_feasible_buyer_offer_turn": None,
        "peak_feasible_seller_utility_usd": None,
        "peak_feasible_seller_offer_turn": None,
        "peak_feasible_joint_utility_usd": None,
        "peak_feasible_offer_turn": None,
        "pareto_efficient_offer_ever_seen": None,
        "first_pareto_efficient_offer_turn": None,
        "offers_after_first_pareto_efficient": None,
        "buyer_utility_delta_peak_to_final_usd": None,
        "seller_utility_delta_peak_to_final_usd": None,
        "joint_utility_delta_peak_to_final_usd": None,
        "accepted_offer_retained_peak_buyer_utility": None,
        "accepted_offer_retained_peak_seller_utility": None,
        "accepted_offer_retained_peak_joint_utility": None,
    }
    if not feasible:
        return result

    peaks: dict[str, tuple[Decimal, dict[str, Any]]] = {}
    for role, key in (
        ("buyer", "buyer_gain"),
        ("seller", "seller_gain"),
        ("joint", "joint_gain"),
    ):
        value = max(row["economics"][key] for row in feasible)
        row = next(row for row in feasible if row["economics"][key] == value)
        peaks[role] = (value, row)
    pareto_rows = [
        row for row in feasible if _offer_pareto_efficient(row["economics"]) is True
    ]
    result.update({
        "peak_feasible_buyer_utility_usd": float(peaks["buyer"][0]),
        "peak_feasible_buyer_offer_turn": peaks["buyer"][1]["turn"],
        "peak_feasible_seller_utility_usd": float(peaks["seller"][0]),
        "peak_feasible_seller_offer_turn": peaks["seller"][1]["turn"],
        "peak_feasible_joint_utility_usd": float(peaks["joint"][0]),
        "peak_feasible_offer_turn": peaks["joint"][1]["turn"],
        "pareto_efficient_offer_ever_seen": int(bool(pareto_rows)),
        "first_pareto_efficient_offer_turn": (
            pareto_rows[0]["turn"] if pareto_rows else None
        ),
        "offers_after_first_pareto_efficient": (
            sum(row["turn"] > pareto_rows[0]["turn"] for row in protocol_offers)
            if pareto_rows else None
        ),
    })
    if not facts.get("valid_agreement"):
        return result
    final_values = {
        "buyer": _decimal(facts.get("buyer_batna_gain_usd")),
        "seller": _decimal(facts.get("seller_batna_gain_usd")),
        "joint": _decimal(facts.get("joint_batna_gain_usd")),
    }
    for role, final in final_values.items():
        if final is None:
            continue
        peak = peaks[role][0]
        result[f"{role}_utility_delta_peak_to_final_usd"] = float(final - peak)
        result[f"accepted_offer_retained_peak_{role}_utility"] = int(final >= peak)
    return result


def _offer_path_behavior_metrics(
    protocol_offers: list[dict[str, Any]],
) -> dict[str, int | float | None]:
    """Describe observable offer moves after the first feasible offer.

    A later formal offer is an observable attempt, not proof of an agent's
    private intent. ``self_advancing`` means only that the offer would improve
    its author's utility relative to the immediately preceding formal offer.
    Feasibility, realized outcome, and value retention remain separate fields.
    """
    result: dict[str, int | float | None] = {
        "continued_after_first_feasible_offer": None,
        "post_first_feasible_offer_count": None,
        "buyer_post_first_feasible_offer_count": None,
        "seller_post_first_feasible_offer_count": None,
        "feasible_post_first_offer_count": None,
        "infeasible_post_first_offer_count": None,
        "package_change_offer_count": None,
        "price_only_offer_count": None,
        "identical_reoffer_count": None,
        "joint_improving_offer_move_count": None,
        "joint_neutral_offer_move_count": None,
        "joint_harming_offer_move_count": None,
        "buyer_self_advancing_offer_count": None,
        "seller_self_advancing_offer_count": None,
        "buyer_max_requested_utility_gain_from_first_usd": None,
        "seller_max_requested_utility_gain_from_first_usd": None,
    }
    first_index = next(
        (
            index
            for index, row in enumerate(protocol_offers)
            if row.get("economics") and row["economics"]["feasible"] == 1
        ),
        None,
    )
    if first_index is None:
        return result

    path = protocol_offers[first_index:]
    first = path[0]
    later = path[1:]
    result.update({
        "continued_after_first_feasible_offer": int(bool(later)),
        "post_first_feasible_offer_count": len(later),
        "buyer_post_first_feasible_offer_count": sum(
            row["role"] == "buyer" for row in later
        ),
        "seller_post_first_feasible_offer_count": sum(
            row["role"] == "seller" for row in later
        ),
        "feasible_post_first_offer_count": sum(
            bool(row.get("economics")) and row["economics"]["feasible"] == 1
            for row in later
        ),
        "infeasible_post_first_offer_count": sum(
            bool(row.get("economics")) and row["economics"]["feasible"] == 0
            for row in later
        ),
        "package_change_offer_count": 0,
        "price_only_offer_count": 0,
        "identical_reoffer_count": 0,
        "joint_improving_offer_move_count": 0,
        "joint_neutral_offer_move_count": 0,
        "joint_harming_offer_move_count": 0,
        "buyer_self_advancing_offer_count": 0,
        "seller_self_advancing_offer_count": 0,
    })

    requested_gains: dict[str, list[Decimal]] = {"buyer": [], "seller": []}
    first_economics = first["economics"]
    for before, after in pairwise(path):
        package_changed = any(
            before["terms"].get(field) != after["terms"].get(field)
            for field in NEGOTIABLE_ISSUE_FIELDS
        )
        price_changed = (
            before["terms"].get("total_price_usd")
            != after["terms"].get("total_price_usd")
        )
        if package_changed:
            result["package_change_offer_count"] += 1
        elif price_changed:
            result["price_only_offer_count"] += 1
        else:
            result["identical_reoffer_count"] += 1

        before_economics = before.get("economics")
        after_economics = after.get("economics")
        if not before_economics or not after_economics:
            continue
        joint_delta = after_economics["joint_gain"] - before_economics["joint_gain"]
        direction = (
            "improving" if joint_delta > 0
            else "harming" if joint_delta < 0
            else "neutral"
        )
        result[f"joint_{direction}_offer_move_count"] += 1

        author = after["role"]
        author_key = f"{author}_gain"
        author_delta = after_economics[author_key] - before_economics[author_key]
        if author_delta > 0:
            result[f"{author}_self_advancing_offer_count"] += 1
        requested_gains[author].append(
            after_economics[author_key] - first_economics[author_key]
        )

    for role in ("buyer", "seller"):
        gains = requested_gains[role]
        result[f"{role}_max_requested_utility_gain_from_first_usd"] = (
            float(max(gains)) if gains else None
        )
    return result


def _numeric(value: Any) -> int | float | None:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _role_model(run: RunView, role: str) -> str | None:
    roles = run.run.get("roles") or {}
    value = roles.get(role) if isinstance(roles, dict) else None
    model = value.get("model") if isinstance(value, dict) else None
    return str(model).strip() if model else None


def _estimated_api_cost_usd(metrics: Any, model: str | None) -> float | None:
    """Estimate standard-tier token cost from one role's final metrics.

    This is intentionally an estimate rather than a provider-billed amount.
    Aggregate CLI usage cannot determine provider discounts, per-request
    long-context uplifts, or a non-default service tier, so those assumptions
    remain explicit metadata.  Rung 5 only estimates OpenAI model costs;
    DeepSeek and Cursor return ``None`` here by study policy.
    """
    rates = TOKEN_PRICING_USD_PER_MILLION.get(model or "")
    if rates is None:
        return None
    input_tokens = int(metrics.input_tokens)
    output_tokens = int(metrics.output_tokens)
    cache_read_tokens = int(metrics.cache_read_tokens)
    cache_creation_tokens = int(metrics.cache_creation_tokens)
    values = (
        input_tokens,
        output_tokens,
        cache_read_tokens,
        cache_creation_tokens,
    )
    if any(value < 0 for value in values):
        return None
    categorized_input = cache_read_tokens + cache_creation_tokens
    if categorized_input > input_tokens:
        return None
    uncached_input = input_tokens - categorized_input
    cost = (
        Decimal(uncached_input) * rates["input"]
        + Decimal(cache_read_tokens) * rates["cache_read"]
        + Decimal(cache_creation_tokens) * rates["cache_creation"]
        + Decimal(output_tokens) * rates["output"]
    ) / Decimal(1_000_000)
    return float(cost.quantize(Decimal("0.00000001")))


def cost_summary(run: RunView) -> dict[str, Any]:
    """Build a human-facing token and cost summary for any completed run.

    This deliberately does not depend on a successful task evaluation.  A
    failed or timed-out run can still have billable model usage, so the run
    wrapper calls this after every newly-created run artifact.  When token
    accounting is partial, the observed-token estimate is labelled as a lower
    bound rather than presented as a complete bill.
    """
    rows: dict[str, dict[str, Any]] = {}
    for role in ("buyer", "seller"):
        metrics = run.role_metrics(role)
        final_metrics = run.trajectory(role).get("final_metrics") or {}
        model = _role_model(run, role)
        estimated = _estimated_api_cost_usd(metrics, model)
        accounting_complete = bool(final_metrics) and not metrics.partial
        rows[role] = {
            "model": model,
            "input_tokens": metrics.input_tokens,
            "output_tokens": metrics.output_tokens,
            "cache_read_tokens": metrics.cache_read_tokens,
            "cache_creation_tokens": metrics.cache_creation_tokens,
            "framework_total_tokens": metrics.total_tokens,
            "reported_cost_usd": metrics.cost_usd,
            "estimated_api_cost_usd": estimated,
            "accounting_complete": accounting_complete,
            "cost_estimate_complete": accounting_complete and estimated is not None,
        }

    estimates = [rows[role]["estimated_api_cost_usd"] for role in rows]
    estimated_total = (
        float(
            sum(Decimal(str(value)) for value in estimates).quantize(
                Decimal("0.00000001")
            )
        )
        if all(value is not None for value in estimates)
        else None
    )
    reported = [rows[role]["reported_cost_usd"] for role in rows]
    reported_total = (
        float(sum(Decimal(str(value)) for value in reported))
        if all(value is not None for value in reported)
        else None
    )
    total_metrics = run.total_metrics()
    combined = {
        "input_tokens": total_metrics.input_tokens,
        "output_tokens": total_metrics.output_tokens,
        "cache_read_tokens": total_metrics.cache_read_tokens,
        "cache_creation_tokens": total_metrics.cache_creation_tokens,
        "framework_total_tokens": total_metrics.total_tokens,
        "reported_cost_usd": reported_total,
        "estimated_api_cost_usd": estimated_total,
        "accounting_complete": all(rows[role]["accounting_complete"] for role in rows),
        "cost_estimate_complete": all(
            rows[role]["cost_estimate_complete"] for role in rows
        ),
    }
    return {
        "run_dir": str(run.run_dir),
        "status": run.run.get("status"),
        "combined": combined,
        "roles": rows,
        "estimated_api_cost_usd": estimated_total,
        "cost_estimate_complete": all(
            rows[role]["cost_estimate_complete"] for role in rows
        ),
        "reported_cost_usd": reported_total,
        "reported_cost_complete": reported_total is not None,
        "pricing_effective_date": TOKEN_PRICING_EFFECTIVE_DATE,
        "pricing_source": TOKEN_PRICING_SOURCE,
    }


def format_cost_summary(run: RunView) -> str:
    """Render :func:`cost_summary` as an immediately readable terminal block."""
    summary = cost_summary(run)
    lines = [
        "",
        "=== API cost summary ===",
        f"run: {summary['run_dir']}",
        f"status: {summary['status'] or 'unknown'}",
    ]
    combined = summary["combined"]
    lines.extend(
        [
            "combined token usage (buyer + seller):",
            f"  input={combined['input_tokens']}",
            f"  output={combined['output_tokens']}",
            f"  cache_read={combined['cache_read_tokens']}",
            f"  cache_creation={combined['cache_creation_tokens']}",
            f"  framework_total={combined['framework_total_tokens']}",
        ]
    )

    estimated_total = summary["estimated_api_cost_usd"]
    if estimated_total is None:
        total_text = "unavailable"
    elif summary["cost_estimate_complete"]:
        total_text = f"${estimated_total:.8f}"
    else:
        total_text = f">= ${estimated_total:.8f} (lower bound)"
    reported_total = summary["reported_cost_usd"]
    reported_text = (
        f"${reported_total:.8f}"
        if reported_total is not None
        else "unavailable from provider"
    )
    lines.extend(
        [
            f"estimated experiment cost: {total_text}",
            f"provider-reported run total: {reported_text}",
            (
                "pricing assumptions: standard tier, no per-request long-context "
                f"uplift; frozen {summary['pricing_effective_date']}"
            ),
            "billing note: the provider platform bill remains authoritative.",
            "========================",
        ]
    )
    return "\n".join(lines)


class PrincipalInterestTradeoff(Metric):
    """Raw, non-collapsed measures for the interest-seeking negotiation RQ."""

    name = "interest_seeking_tradeoff"
    level = 1
    description = (
        "Deterministic turns, stopping point, BATNA-relative value, issue choice, "
        "resource use, and optional transcript-to-ledger fidelity. Emits l1_score "
        "(V·E·JUR) separately from final_score / composite_score (V·E·JUR·W)."
    )

    async def compute(self, run: RunView):
        if not _is_this_task(run):
            return self.skip("not an interest_seeking_negotiation run")
        facts = _facts(run)
        if not facts:
            return self.skip("eval_result.json has no interest-seeking research facts")

        ledgers = {role: _read_ledger(run, role) for role in ("buyer", "seller")}
        offer_rows: list[tuple[str, dict[str, Any], list[tuple[int, int]]]] = []
        for role, ledger in ledgers.items():
            for event in _outgoing_offer_events(ledger):
                offer_rows.append((role, event, _matching_offer_turns(run, role, event)))

        transcript_offers = _transcript_offers(run)
        protocol_offers = _transcript_offer_terms(run)
        for row in protocol_offers:
            row["economics"] = _offer_economics(row["terms"])
        audited_records_available = bool(offer_rows)
        if audited_records_available:
            offer_count = len(offer_rows)
            unique_message_matches = sum(len(matches) == 1 for _, _, matches in offer_rows)
            exact_offer_messages = sum(
                len(matches) == 1 and matches[0][1] == len(TERM_FIELDS)
                for _, _, matches in offer_rows
            )
            present_terms = sum(
                matches[0][1] if len(matches) == 1 else 0
                for _, _, matches in offer_rows
            )
        else:
            offer_count = len(transcript_offers)
            unique_message_matches = exact_offer_messages = present_terms = 0

        accepted_id = str(facts.get("accepted_offer_id") or "")
        accepted_author = facts.get("accepted_offer_author")
        accepted_offer_turn = None
        if audited_records_available:
            for role, event, matches in offer_rows:
                if str(event.get("offer_id") or "") == accepted_id and role == accepted_author:
                    accepted_offer_turn = matches[0][0] if len(matches) == 1 else None
                    break
        else:
            accepted_row = next(
                (row for row in transcript_offers if row[1] == accepted_id.upper()),
                None,
            )
            if accepted_row:
                accepted_author = accepted_row[0]
                accepted_offer_turn = accepted_row[2]
        acceptance_turn = _acceptance_turn(run, accepted_id, accepted_author)
        total_turns = len(run.turns)

        first_offer = protocol_offers[0] if protocol_offers else None
        feasible_offers = [
            row for row in protocol_offers
            if row.get("economics") and row["economics"]["feasible"] == 1
        ]
        first_feasible = feasible_offers[0] if feasible_offers else None
        first_economics = (
            first_feasible.get("economics") if first_feasible else None
        )
        issue_changes = 0
        for before, after in pairwise(protocol_offers):
            issue_changes += sum(
                before["terms"].get(field) != after["terms"].get(field)
                for field in TERM_FIELDS
                if field not in {"licensed_seats", "total_price_usd"}
            )
        first_price = (
            _decimal(first_offer["terms"].get("total_price_usd"))
            if first_offer else None
        )
        first_reference = (
            ROLE_REFERENCE_USD.get(first_offer["role"]) if first_offer else None
        )

        messages_from = {
            role: sum(turn.role == role for turn in run.turns) for role in ("buyer", "seller")
        }
        if audited_records_available:
            offer_counts = {
                role: sum(row_role == role for row_role, _, _ in offer_rows)
                for role in ("buyer", "seller")
            }
        else:
            offer_counts = {
                role: sum(row_role == role for row_role, _, _ in transcript_offers)
                for role in ("buyer", "seller")
            }

        role_metrics = {role: run.role_metrics(role) for role in ("buyer", "seller")}
        total_metrics = run.total_metrics()
        final_metrics = {
            role: (run.trajectory(role).get("final_metrics") or {})
            for role in ("buyer", "seller")
        }
        accounting_complete = {
            role: bool(final_metrics[role]) and not role_metrics[role].partial
            for role in ("buyer", "seller")
        }
        role_models = {role: _role_model(run, role) for role in ("buyer", "seller")}
        estimated_costs = {
            role: _estimated_api_cost_usd(role_metrics[role], role_models[role])
            for role in ("buyer", "seller")
        }
        cost_estimate_complete = {
            role: accounting_complete[role] and estimated_costs[role] is not None
            for role in ("buyer", "seller")
        }

        def ratio(numerator: int, denominator: int) -> float | None:
            return numerator / denominator if denominator else None

        scores: dict[str, int | float | None] = {
            "messages_to_termination": total_turns,
            "buyer_messages": messages_from["buyer"],
            "seller_messages": messages_from["seller"],
            "formal_offer_count": offer_count,
            "buyer_offer_count": offer_counts["buyer"],
            "seller_offer_count": offer_counts["seller"],
            "agreement_reached": 1 if facts.get("status") == "agreed" else 0,
            "accepted_offer_turn": accepted_offer_turn,
            "acceptance_turn": acceptance_turn,
            "messages_offer_to_acceptance": (
                acceptance_turn - accepted_offer_turn
                if acceptance_turn is not None and accepted_offer_turn is not None
                else None
            ),
            "messages_after_acceptance": (
                total_turns - acceptance_turn if acceptance_turn is not None else None
            ),
            "transcript_offer_id_coverage": (
                unique_message_matches / offer_count
                if audited_records_available and offer_count else None
            ),
            "transcript_offer_term_fidelity": (
                present_terms / (offer_count * len(TERM_FIELDS))
                if audited_records_available and offer_count else None
            ),
            "transcript_exact_offer_rate": (
                exact_offer_messages / offer_count
                if audited_records_available and offer_count else None
            ),
            "acceptance_transcript_supported": (
                1 if acceptance_turn is not None else 0 if facts.get("status") == "agreed" else None
            ),
            "protocol_json_offer_count": len(protocol_offers),
            "first_formal_offer_turn": first_offer["turn"] if first_offer else None,
            "first_formal_offer_price_usd": float(first_price) if first_price is not None else None,
            "first_offer_minus_author_reference_usd": (
                float(first_price - first_reference)
                if first_price is not None and first_reference is not None else None
            ),
            "first_feasible_offer_turn": first_feasible["turn"] if first_feasible else None,
            "first_feasible_offer_accepted": (
                int(first_feasible["offer_id"] == accepted_id.upper())
                if first_feasible and accepted_id else None
            ),
            "formal_offers_after_first_feasible": (
                sum(row["turn"] > first_feasible["turn"] for row in protocol_offers)
                if first_feasible else None
            ),
            "nonprice_issue_changes_across_offers": issue_changes if protocol_offers else None,
            "wall_clock_duration_s": _numeric(run.duration_s),
        }
        scores.update(_offer_change_metrics(first_economics, facts))
        scores.update(_offer_path_quality_metrics(protocol_offers, facts))
        scores.update(_offer_path_behavior_metrics(protocol_offers))
        scores.update(
            _agentic_contribution_metrics(
                run,
                protocol_offers,
                first_feasible,
                accepted_offer_id=accepted_id,
                accepted_offer_author=accepted_author,
                acceptance_turn=acceptance_turn,
            )
        )
        for key in (
            "total_price_usd",
            "buyer_package_value_usd",
            "seller_minimum_price_usd",
            "buyer_batna_gain_usd",
            "seller_batna_gain_usd",
            "joint_batna_gain_usd",
            "buyer_utility_realization",
            "seller_utility_realization",
            "joint_value_realization",
            "uncreated_joint_value_usd",
            "buyer_execution_quality",
            "seller_execution_quality",
            "recorded_offer_fidelity",
        ):
            scores[key] = _numeric(facts.get(key))
        pareto = facts.get("pareto_efficient")
        scores["pareto_efficient"] = int(pareto) if isinstance(pareto, bool) else None
        for key in (
            "guided_migration", "priority_onboarding",
            "premium_incident_response", "anonymized_reference_right",
        ):
            value = facts.get(key)
            scores[key] = int(value) if isinstance(value, bool) else None
        scores["quarterly_billing"] = (
            1 if facts.get("billing_schedule") == "quarterly_net_30"
            else 0 if facts.get("billing_schedule") else None
        )

        valid_agreement = bool(facts.get("valid_agreement"))
        if valid_agreement:
            buyer_utility = _numeric(facts.get("buyer_utility_realization"))
            seller_utility = _numeric(facts.get("seller_utility_realization"))
            joint_l1 = _numeric(facts.get("joint_value_realization"))
            buyer_execution = _numeric(facts.get("buyer_execution_quality"))
            seller_execution = _numeric(facts.get("seller_execution_quality"))
            if all(
                value is not None
                for value in (
                    buyer_utility,
                    seller_utility,
                    joint_l1,
                    buyer_execution,
                    seller_execution,
                )
            ):
                scores["principal_balance"] = _principal_balance(
                    buyer_utility,
                    seller_utility,
                )
                scores["joint_utility_realization"] = joint_l1
                # Compatibility alias for existing result consumers. The
                # scored outcome is now Joint Utility Realization directly.
                scores["principal_outcome"] = joint_l1
                # L1 components: validity (gate), execution, JUR.
                scores["validity"] = 1.0
                scores["execution"] = _mean_available(
                    buyer_execution, seller_execution
                )
                scores["l1_score"] = _l1_score(
                    valid=True,
                    buyer_execution=buyer_execution,
                    seller_execution=seller_execution,
                    joint_utility_realization=joint_l1,
                )
                # Final Score = L1 × W. composite_score is a deprecated alias.
                scores["final_score"] = _candidate_overall_score(
                    valid=True,
                    buyer_execution=buyer_execution,
                    seller_execution=seller_execution,
                    joint_utility_realization=joint_l1,
                    buyer_behavior=scores["buyer_behavior"],
                    seller_behavior=scores["seller_behavior"],
                )
                scores["composite_score"] = scores["final_score"]
            else:
                scores["principal_balance"] = None
                scores["joint_utility_realization"] = None
                scores["principal_outcome"] = None
                scores["validity"] = None
                scores["execution"] = None
                scores["l1_score"] = None
                scores["final_score"] = None
                scores["composite_score"] = None
        else:
            scores["principal_balance"] = None
            scores["joint_utility_realization"] = None
            scores["principal_outcome"] = None
            scores["validity"] = 0.0
            scores["execution"] = None
            scores["l1_score"] = 0.0
            scores["final_score"] = 0.0
            scores["composite_score"] = 0.0

        for role in ("buyer", "seller"):
            metrics = role_metrics[role]
            scores.update(
                {
                    f"{role}_input_tokens": metrics.input_tokens,
                    f"{role}_output_tokens": metrics.output_tokens,
                    f"{role}_cache_read_tokens": metrics.cache_read_tokens,
                    f"{role}_cache_creation_tokens": metrics.cache_creation_tokens,
                    f"{role}_framework_total_tokens": metrics.total_tokens,
                    f"{role}_reported_cost_usd": metrics.cost_usd,
                    f"{role}_estimated_api_cost_usd": estimated_costs[role],
                    f"{role}_cost_estimate_complete": int(
                        cost_estimate_complete[role]
                    ),
                    f"{role}_metrics_partial": int(
                        metrics.partial or not final_metrics[role]
                    ),
                    f"{role}_accounting_complete": int(accounting_complete[role]),
                    f"{role}_tokens_per_delivered_message": ratio(
                        metrics.total_tokens, messages_from[role]
                    ),
                    f"{role}_tokens_per_formal_offer": ratio(
                        metrics.total_tokens, offer_counts[role]
                    ),
                }
            )
        run_accounting_complete = all(accounting_complete.values())
        run_estimated_cost = (
            float(
                sum(
                    Decimal(str(value))
                    for value in estimated_costs.values()
                    if value is not None
                ).quantize(Decimal("0.00000001"))
            )
            if all(value is not None for value in estimated_costs.values())
            else None
        )
        scores.update(
            {
                "run_input_tokens": total_metrics.input_tokens,
                "run_output_tokens": total_metrics.output_tokens,
                "run_cache_read_tokens": total_metrics.cache_read_tokens,
                "run_cache_creation_tokens": total_metrics.cache_creation_tokens,
                "run_framework_total_tokens": total_metrics.total_tokens,
                "run_reported_cost_usd": total_metrics.cost_usd,
                "run_estimated_api_cost_usd": run_estimated_cost,
                "cost_estimate_complete": int(
                    all(cost_estimate_complete.values())
                ),
                "metrics_partial": int(
                    total_metrics.partial or not run_accounting_complete
                ),
                "accounting_complete": int(run_accounting_complete),
                "reported_cost_complete": int(
                    all(role_metrics[role].cost_usd is not None for role in role_metrics)
                ),
                "run_tokens_per_delivered_message": ratio(
                    total_metrics.total_tokens, total_turns
                ),
                "run_tokens_per_formal_offer": ratio(
                    total_metrics.total_tokens, offer_count
                ),
            }
        )

        roles = {
            "buyer": RoleScore(
                scores={
                    "messages": messages_from["buyer"],
                    "formal_offers": offer_counts["buyer"],
                    "batna_gain_usd": scores.get("buyer_batna_gain_usd"),
                    "utility_realization": scores.get("buyer_utility_realization"),
                    "execution_quality": scores.get("buyer_execution_quality"),
                    "vco": scores.get("buyer_vco"),
                    "dpe": scores.get("buyer_dpe"),
                    "fpd": scores.get("buyer_fpd"),
                    "sr": scores.get("buyer_sr"),
                    "behavior": scores.get("buyer_behavior"),
                    "behavior_applicable_count": scores.get(
                        "buyer_behavior_applicable_count"
                    ),
                    "input_tokens": scores.get("buyer_input_tokens"),
                    "output_tokens": scores.get("buyer_output_tokens"),
                    "cache_read_tokens": scores.get("buyer_cache_read_tokens"),
                    "cache_creation_tokens": scores.get("buyer_cache_creation_tokens"),
                    "framework_total_tokens": scores.get("buyer_framework_total_tokens"),
                    "reported_cost_usd": scores.get("buyer_reported_cost_usd"),
                    "estimated_api_cost_usd": scores.get(
                        "buyer_estimated_api_cost_usd"
                    ),
                    "cost_estimate_complete": scores.get(
                        "buyer_cost_estimate_complete"
                    ),
                    "metrics_partial": scores.get("buyer_metrics_partial"),
                    "accounting_complete": scores.get("buyer_accounting_complete"),
                }
            ),
            "seller": RoleScore(
                scores={
                    "messages": messages_from["seller"],
                    "formal_offers": offer_counts["seller"],
                    "batna_gain_usd": scores.get("seller_batna_gain_usd"),
                    "utility_realization": scores.get("seller_utility_realization"),
                    "execution_quality": scores.get("seller_execution_quality"),
                    "vco": scores.get("seller_vco"),
                    "dpe": scores.get("seller_dpe"),
                    "fpd": scores.get("seller_fpd"),
                    "sr": scores.get("seller_sr"),
                    "behavior": scores.get("seller_behavior"),
                    "behavior_applicable_count": scores.get(
                        "seller_behavior_applicable_count"
                    ),
                    "input_tokens": scores.get("seller_input_tokens"),
                    "output_tokens": scores.get("seller_output_tokens"),
                    "cache_read_tokens": scores.get("seller_cache_read_tokens"),
                    "cache_creation_tokens": scores.get("seller_cache_creation_tokens"),
                    "framework_total_tokens": scores.get("seller_framework_total_tokens"),
                    "reported_cost_usd": scores.get("seller_reported_cost_usd"),
                    "estimated_api_cost_usd": scores.get(
                        "seller_estimated_api_cost_usd"
                    ),
                    "cost_estimate_complete": scores.get(
                        "seller_cost_estimate_complete"
                    ),
                    "metrics_partial": scores.get("seller_metrics_partial"),
                    "accounting_complete": scores.get("seller_accounting_complete"),
                }
            ),
        }
        outcome = "agreement" if scores["agreement_reached"] else "no agreement"
        return self.result(
            scores=scores,
            roles=roles,
            summary=(
                f"{outcome}; {total_turns} messages; {offer_count} formal offers. "
                f"l1_score={scores.get('l1_score')}; "
                f"final_score={scores.get('final_score')}; "
                "Final Score is not paper L1. "
                "outcome, behavior, and resource diagnostics are also retained."
            ),
            metadata={
                "accepted_offer_id": accepted_id or None,
                "accepted_offer_author": accepted_author,
                "ledgers_available": {role: ledger is not None for role, ledger in ledgers.items()},
                "formal_offer_count_source": (
                    "audited_ledgers" if audited_records_available else "transcript_offer_ids"
                ),
                "protocol_offer_path": [
                    {
                        "role": row["role"],
                        "offer_id": row["offer_id"],
                        "turn": row["turn"],
                        "terms": row["terms"],
                        "economics": {
                            key: float(value)
                            for key, value in (row.get("economics") or {}).items()
                        },
                    }
                    for row in protocol_offers
                ],
                "behavior_metric_scope": (
                    "observable formal offers after the first feasible offer; "
                    "self-advancing describes proposed utility versus the prior "
                    "offer and does not infer private intent"
                ),
                "metric_design": (
                    "l1_score = validity × execution × joint-utility-realization; "
                    "final_score / composite_score additionally multiplies behavior (W). "
                    "Final Score is not paper L1"
                ),
                "composite_score_scope": (
                    "deprecated alias of final_score (V·E·JUR·W); paper L1 is "
                    "l1_score (V·E·JUR). The framework top-level scalar remains JUR"
                ),
                "resource_accounting_source": "trajectory_<role>.json final_metrics",
                "framework_total_token_definition": (
                    "input + output + cache_read + cache_creation"
                ),
                "estimated_cost_models": role_models,
                "estimated_cost_pricing": {
                    model: {key: float(value) for key, value in rates.items()}
                    for model, rates in TOKEN_PRICING_USD_PER_MILLION.items()
                },
                "estimated_cost_pricing_unit": "USD per 1M tokens",
                "estimated_cost_pricing_effective_date": (
                    TOKEN_PRICING_EFFECTIVE_DATE
                ),
                "estimated_cost_pricing_source": TOKEN_PRICING_SOURCE,
                "estimated_cost_assumptions": (
                    "OpenAI models only; standard service tier; no per-request "
                    "long-context uplift; input includes cache-read and "
                    "cache-creation categories; DeepSeek and Cursor costs are "
                    "intentionally not estimated"
                ),
                "estimated_cost_is_task_score": False,
            },
        )


def _replicate_label(run: RunView, facts: dict[str, Any]) -> str:
    variant = str(facts.get("variant") or "unknown").upper()
    path = str(run.run_dir).lower()
    replicate = next(
        (number for number in (1, 2, 3) if f"r0{number}" in path),
        None,
    )
    return f"{variant} · r{replicate or '?'}"


def _trajectory_result_label(scores: dict[str, Any]) -> str | None:
    """Summarize the observed path from deterministic trajectory metrics."""
    later_offers = scores.get("formal_offers_after_first_feasible")
    first_pareto = scores.get("first_offer_pareto_efficient")
    if later_offers == 0:
        if first_pareto == 1:
            return "直接接受；起点 Pareto"
        if first_pareto == 0:
            return "直接接受；起点非 Pareto"
        return "直接接受"

    joint_delta = scores.get("joint_utility_delta_first_to_final_usd")
    if not isinstance(joint_delta, (int, float)):
        return None
    if joint_delta > 0:
        return "联合价值增加"
    if joint_delta < 0:
        peak_delta = scores.get("joint_utility_delta_peak_to_final_usd")
        if isinstance(peak_delta, (int, float)) and peak_delta < 0:
            return "联合价值降低；放弃过程峰值"
        return "联合价值降低"

    buyer_delta = scores.get("buyer_utility_delta_first_to_final_usd")
    seller_delta = scores.get("seller_utility_delta_first_to_final_usd")
    if buyer_delta == 0 and seller_delta == 0:
        return "往返后回到起点"
    if scores.get("package_change_offer_count") == 0:
        return "价格再分配"
    return "价值再分配"


def _write_behavior_summary(
    path: Path,
    runs: list[RunView],
    reports: list[MetricReport],
) -> None:
    """Write a reproducible flat table for the Rung 2 trajectory analysis."""
    metric_fields = (
        "buyer_vco", "buyer_dpe", "buyer_fpd", "buyer_sr", "buyer_behavior",
        "seller_vco", "seller_dpe", "seller_fpd", "seller_sr", "seller_behavior",
        "pair_behavior", "behavior_multiplier", "principal_balance",
        "joint_utility_realization", "principal_outcome",
        "validity", "execution", "l1_score", "final_score", "composite_score",
    )
    fieldnames = ("trajectory", *metric_fields, "trajectory_result", "run_dir")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, delimiter="\t")
        writer.writeheader()
        rows: list[dict[str, Any]] = []
        for run, report in zip(runs, reports, strict=True):
            dumped = report.model_dump(mode="json")
            result = (dumped.get("results") or {}).get(PrincipalInterestTradeoff.name) or {}
            scores = result.get("scores") or {}
            facts = _facts(run)
            row = {
                "trajectory": _replicate_label(run, facts),
                **{field: scores.get(field) for field in metric_fields},
                "trajectory_result": _trajectory_result_label(scores),
                "run_dir": str(run.run_dir),
            }
            rows.append(row)
        rows.sort(key=lambda row: row["trajectory"])
        writer.writerows(rows)


async def _run(args: argparse.Namespace) -> int:
    missing = [path for path in args.paths if not path.exists()]
    if missing:
        print(f"error: no such path: {missing[0]}", file=sys.stderr)
        return 1
    runs = [
        run
        for path in args.paths
        for run in iter_runs(path)
        if _is_this_task(run)
    ]
    if not runs:
        print(f"error: no {TASK_SUFFIX} runs under the supplied paths", file=sys.stderr)
        return 1
    reports: list[MetricReport] = []
    for run in runs:
        report = await run_metrics(run, [PrincipalInterestTradeoff()])
        reports.append(report)
        if not args.stdout:
            path = write_metrics(report, run.run_dir, merge=True)
            print(f"wrote {path}")
            print(format_cost_summary(run))
    if args.stdout:
        print(
            json.dumps(
                [report.model_dump(mode="json") for report in reports],
                indent=2,
                ensure_ascii=False,
            )
        )
    if args.summary_tsv:
        _write_behavior_summary(args.summary_tsv, runs, reports)
        print(f"wrote {args.summary_tsv}")
    return 0 if all(report.ok for report in reports) else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Compute deterministic metrics for the interest-seeking negotiation task."
    )
    parser.add_argument(
        "paths",
        nargs="+",
        type=Path,
        help="One or more run directories or trees of runs.",
    )
    parser.add_argument(
        "--stdout",
        action="store_true",
        help="Print JSON instead of merging the result into each metrics.json.",
    )
    parser.add_argument(
        "--summary-tsv",
        type=Path,
        help="Write one deterministic Rung 2 behavior/outcome row per run.",
    )
    return asyncio.run(_run(parser.parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
