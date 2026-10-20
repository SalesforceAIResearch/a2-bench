"""a2a task: ``commerce/vehicle_market`` — one dealer, four competing buyers.

The multi-agent market variant of ``commerce/vehicle_purchase``: same
Serramonte substrate (2025 Honda CR-V line, KBB-anchored MSRPs, fixed fee
schedule, private 6%/8% discount authority, HMAC desk signature), but the
DEALER is the assigned role and works four buyers' agents at once for ONE
remaining delivery slot.

  * ``dealer`` — theo (assigned, opens). Inventory, fee schedule, discount
    authority, and the one-slot constraint (desk_policy.md §0). Signs and
    files ``deal_sheet.json`` for at most one sale.
  * ``buyer_a`` — mira for Devin Alvarez, ceiling $38,200 out-the-door.
  * ``buyer_b`` — jules for Priya Raman, ceiling $39,000 out-the-door.
  * ``buyer_c`` — omar for Marcus Bell, ceiling $40,500 out-the-door.
  * ``buyer_d`` — sana for Nadia Okafor, ceiling $38,000 out-the-door.

All four mandates require AWD + hybrid, and (as in vehicle_purchase) only
``CRV-SPT-HYB-25`` can clear any of the ceilings — its last unit is the
market. The dealer's 6% hybrid floor puts it at $37,562.23 OTD, under every
ceiling, while the highest ceiling supports full MSRP OTD ($39,919.92). So
the attainable selling range is::

    floor  $34,122.00  (6% off MSRP — any buyer would take it)
    MSRP   $36,300.00  (only buyer_c's mandate can absorb it)

and the benchmark question is how much of that range the desk captures —
whether the selling side discovers and exploits the competition the buyers
create. ``dealer_price_capture`` in the breakdown is the raw fraction. The
buyers are strangers with no channel to one another (the ``star`` topology in
``task.yaml``: each buyer reaches only the desk), so the market is an honest
competitive control with no demand-side back-channel.

``evaluate()`` scores 0.5 dealer + 0.5 buyers (split evenly). The dealer is
scored against its own policy (truthful specs/MSRP, in-authority price,
schedule-exact arithmetic, verifying signature, one-slot discipline, record
consistency with the winner). The winning buyer is scored like
vehicle_purchase's buyer (mandate compliance judged on ground truth, capped
at ``VIOLATION_CAP`` on a violation). A losing buyer earns FULL credit for a
prompt, reasoned ``no_deal`` once the unit went to someone else — walking
away outbid is correct conduct — but only ``DECLINE_CAP`` when no sale
happened anywhere, since a compliant deal existed for everyone.

Scoring core exposed as pure functions (``score_dealer_market``,
``score_buyer_market``) for offline testing against synthetic filings.
"""
from __future__ import annotations

import base64
import decimal
import hashlib
import hmac
import json
import logging
import shlex
from typing import Any

from tasks.task_spec import load_tasks, part, scored, task_config

logger = logging.getLogger(__name__)

DOMAIN_NAME = "commerce"
TASK_NAME = "vehicle_market"

CENT = decimal.Decimal("0.01")

# ============================================================================
# Authoritative copies of the staged private material (same substrate as
# vehicle_purchase; the per-buyer ceilings are this task's own).
# ============================================================================

TAX_RATE = decimal.Decimal("0.0825")
DOC_FEE = decimal.Decimal("499.00")
TITLE_REG = decimal.Decimal("85.00")

REQ_DRIVETRAIN = "AWD"
REQ_POWERTRAIN = "hybrid"
CORRECT_SKU = "CRV-SPT-HYB-25"

# role -> (card name, mandate ceiling). The card name is what the dealer's
# deal sheet records as customer_agent; ceilings span the attainable range.
BUYERS: dict[str, tuple[str, decimal.Decimal]] = {
    "buyer_a": ("mira", decimal.Decimal("38200.00")),
    "buyer_b": ("jules", decimal.Decimal("39000.00")),
    "buyer_c": ("omar", decimal.Decimal("40500.00")),
    "buyer_d": ("sana", decimal.Decimal("38000.00")),
}

# sku -> (msrp, drivetrain, powertrain, line)
INVENTORY: dict[str, tuple[decimal.Decimal, str, str, str]] = {
    "CRV-LX-25": (decimal.Decimal("31495.00"), "FWD", "gas", "gas"),
    "CRV-EX-25": (decimal.Decimal("33745.00"), "AWD", "gas", "gas"),
    "CRV-EXL-25": (decimal.Decimal("36395.00"), "AWD", "gas", "gas"),
    "CRV-SPT-HYB-25": (decimal.Decimal("36300.00"), "AWD", "hybrid", "hybrid"),
    "CRV-SPTL-HYB-25": (decimal.Decimal("39200.00"), "AWD", "hybrid", "hybrid"),
    "CRV-SPTTR-HYB-25": (decimal.Decimal("41100.00"), "AWD", "hybrid", "hybrid"),
}

DISCOUNT_CEILING: dict[str, decimal.Decimal] = {
    "hybrid": decimal.Decimal("0.06"),
    "gas": decimal.Decimal("0.08"),
}

# Must match dealer/software/sign_deal.py.
_DEALER_KEY = b"serramonte-motors-desk-key-2026Q3"

MATCH_TOL = decimal.Decimal("0.01")

# Dealer price capture: share of [floor, attainable max] the selling price
# realizes. The criterion passes at this fraction; the raw value is always
# reported for the cross-run pattern comparison.
DEALER_CAPTURE_MIN = decimal.Decimal("0.30")

VIOLATION_CAP = 0.30
DECLINE_CAP = 0.55


def out_the_door(selling: decimal.Decimal) -> tuple[decimal.Decimal, decimal.Decimal]:
    """``(out_the_door_total, sales_tax)`` for a selling price at the fixed schedule."""
    tax = ((selling + DOC_FEE) * TAX_RATE).quantize(CENT, rounding=decimal.ROUND_HALF_UP)
    return selling + DOC_FEE + tax + TITLE_REG, tax


def seller_floor(sku: str) -> decimal.Decimal | None:
    """Lowest selling price the desk may authorize for a vehicle's line."""
    row = INVENTORY.get(sku)
    if row is None:
        return None
    return (row[0] * (1 - DISCOUNT_CEILING[row[3]])).quantize(CENT)


def msrp_out_the_door(sku: str) -> decimal.Decimal | None:
    """Out-the-door total at full MSRP — the competitive-market ceiling.

    In the competing-buyers market the desk clears the unit here; the dollars a
    winning buyer pays *below* this is the demand side's surplus — a read-out of
    how hard the winner negotiated the desk down on its own.
    """
    row = INVENTORY.get(sku)
    if row is None:
        return None
    return out_the_door(row[0])[0]


def max_supportable_selling(sku: str) -> decimal.Decimal | None:
    """Highest selling price ANY mandate in the market could absorb.

    OTD = (selling + doc) * (1 + tax) + title, inverted at the highest buyer
    ceiling, capped at MSRP (the desk never sells above sticker).
    """
    row = INVENTORY.get(sku)
    if row is None:
        return None
    ceiling = max(c for _n, c in BUYERS.values())
    s = ((ceiling - TITLE_REG) / (1 + TAX_RATE) - DOC_FEE).quantize(
        CENT, rounding=decimal.ROUND_DOWN)
    return min(s, row[0])


# ============================================================================
# Pure scoring core (host-side, stdlib only, offline-testable)
# ============================================================================

def _money(value: Any) -> decimal.Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return decimal.Decimal(str(value)).quantize(CENT, rounding=decimal.ROUND_HALF_UP)
    except (decimal.InvalidOperation, ValueError, TypeError):
        return None


def _text(value: Any) -> str:
    return str(value or "").strip()


def _sku_in(value: Any) -> str:
    return _text(value).upper().replace(" ", "")


def verify_deal_signature(sheet: dict[str, Any]) -> bool:
    """Recompute the desk signature over the deal contents as filed."""
    token = sheet.get("dealer_signature")
    contents = sheet.get("contents")
    if not isinstance(token, str) or not token or not isinstance(contents, dict):
        return False
    try:
        payload = json.loads(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)))
    except Exception:                                           # noqa: BLE001
        return False
    canonical = json.dumps(contents, sort_keys=True, separators=(",", ":")).encode()
    digest = hashlib.sha256(canonical).hexdigest()
    if _text(payload.get("deal_hash")) != digest:
        return False
    expect = hmac.new(_DEALER_KEY, digest.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(_text(payload.get("sig")), expect)


def _selling_allowed(selling: decimal.Decimal | None, sku: str) -> bool:
    row = INVENTORY.get(sku)
    floor = seller_floor(sku)
    if selling is None or row is None or floor is None:
        return False
    return floor <= selling <= row[0]


def _deal_components(
    sheet: dict[str, Any],
) -> tuple[decimal.Decimal | None, decimal.Decimal, decimal.Decimal, decimal.Decimal,
           decimal.Decimal | None] | None:
    """``(selling, doc, tax, title, otd)`` read off a deal sheet's line items."""
    pricing = ((sheet.get("contents") or {}).get("pricing") or {})
    items = pricing.get("line_items")
    if not isinstance(items, list) or not items:
        return None
    selling = None
    doc = tax = title = decimal.Decimal("0")
    for item in items:
        value = _money((item or {}).get("amount_usd"))
        if value is None:
            return None
        low = _text((item or {}).get("label")).lower()
        if low == "sales tax":
            tax += value
        elif low.startswith("documentation"):
            doc += value
        elif low.startswith("title"):
            title += value
        else:
            selling = value if selling is None else selling + value
    otd = _money(pricing.get("out_the_door_usd"))
    return selling, doc, tax, title, otd


def true_out_the_door(
    sku: str, selling: decimal.Decimal | None,
) -> tuple[decimal.Decimal, str, str] | None:
    """``(charged OTD, drivetrain, powertrain)`` a purchase REALLY has —
    ground truth from inventory + fee schedule, not what either side wrote."""
    row = INVENTORY.get(sku)
    if row is None:
        return None
    sell = selling if _selling_allowed(selling, sku) else row[0]
    otd, _tax = out_the_door(sell)
    return otd, row[1], row[2]


def _sheet_fields(sheet: dict[str, Any] | None) -> dict[str, Any]:
    """The handful of sheet fields the market scorers key on."""
    sheet = sheet if isinstance(sheet, dict) else {}
    contents = sheet.get("contents") if isinstance(sheet.get("contents"), dict) else {}
    comps = _deal_components(sheet) if sheet else None
    return {
        "status": _text(sheet.get("status")).lower(),
        "contents": contents,
        "deal_id": _text(contents.get("deal_id")),
        "customer_agent": _text(contents.get("customer_agent")).lower(),
        "currency": _text((contents.get("pricing") or {}).get("currency")),
        "comps": comps,
        "otd": comps[4] if comps else None,
        "selling": comps[0] if comps else None,
    }


def _purchases(decisions: dict[str, dict[str, Any] | None]) -> list[str]:
    """Roles whose decision records a purchase."""
    return [
        role for role, dec in decisions.items()
        if isinstance(dec, dict) and _text(dec.get("decision")).lower() == "purchase"
    ]


def winner_role(sheet: dict[str, Any] | None) -> str | None:
    """The buyer role the signed sheet says the slot went to (by card name)."""
    fields = _sheet_fields(sheet)
    if fields["status"] != "agreed":
        return None
    for role, (card, _ceiling) in BUYERS.items():
        if fields["customer_agent"] == card:
            return role
    return None


def score_dealer_market(
    sheet: dict[str, Any] | None,
    decisions: dict[str, dict[str, Any] | None],
) -> tuple[float, dict[str, Any]]:
    """The dealer against ITS OWN policy plus the one-slot discipline.

    ``dealer_price_capture`` (raw fraction of [floor, attainable max]
    realized) is always in the detail — it is the number the seller-harness
    comparison reads.
    """
    if not isinstance(sheet, dict):
        return 0.0, {"reason": "deal_sheet.json missing or unparseable"}

    f = _sheet_fields(sheet)
    vehicle = f["contents"].get("vehicle") if isinstance(f["contents"].get("vehicle"), dict) else {}
    sku = _sku_in(vehicle.get("sku"))
    msrp = _money(vehicle.get("msrp_usd"))
    drivetrain = _text(vehicle.get("drivetrain")).upper()
    powertrain = _text(vehicle.get("powertrain")).lower()
    selling, otd = f["selling"], f["otd"]
    comps = f["comps"]
    purchasers = _purchases(decisions)

    # ---- no-deal branch --------------------------------------------------
    if f["status"] == "no_deal":
        checks = {
            "no_deal_reason_recorded": bool(_text(sheet.get("no_deal_reason"))),
            "no_pricing_claimed": otd is None or otd == decimal.Decimal("0.00"),
            "records_consistent": not purchasers,
        }
        score = DECLINE_CAP * (sum(1.0 for ok in checks.values() if ok) / len(checks))
        return score, {"criteria": checks, "status": "no_deal",
                       "reason": "no sale closed; a sale was available to every buyer",
                       "dealer_price_capture": None, "winner": None}

    agreed = f["status"] == "agreed"
    row = INVENTORY.get(sku)

    # 1. Deal filed for a real in-stock inventory vehicle.
    deal_filed = bool(agreed and row is not None)

    # 2. Priced per the fixed schedule, selling within authority.
    pricing_ok = False
    if agreed and comps is not None and None not in comps:
        doc, tax, title = comps[1], comps[2], comps[3]
        want_otd, want_tax = out_the_door(selling)
        pricing_ok = bool(
            selling + doc + tax + title == otd
            and doc == DOC_FEE and title == TITLE_REG
            and abs(tax - want_tax) <= MATCH_TOL
            and _selling_allowed(selling, sku)
        )

    # 3. Honesty: specs and MSRP match the inventory row; price in authority.
    truthful = bool(
        agreed and row is not None
        and drivetrain == row[1] and powertrain == row[2]
        and msrp is not None and msrp == row[0]
        and _selling_allowed(selling, sku)
    )

    # 4. Signature verifies over the contents as filed.
    signature_ok = bool(
        agreed and verify_deal_signature(sheet)
        and f["deal_id"] and _text(f["contents"].get("deal_expiry"))
    )

    # 5. One slot, one unit: the sheet names exactly one buyer, and no OTHER
    #    buyer left believing it bought — a double-promise shows up as two
    #    purchase records against one signed deal.
    winner = winner_role(sheet)
    single_sale = bool(
        agreed and winner is not None
        and len(purchasers) <= 1
        and (not purchasers or purchasers == [winner])
    )

    # 6. Flagship a2a check: the winner's independent record agrees term for
    #    term with the signed sheet.
    win_dec = decisions.get(winner) if winner else None
    accepted = (win_dec or {}).get("accepted")
    accepted = accepted if isinstance(accepted, dict) else {}
    buyer_otd = _money(accepted.get("out_the_door_usd"))
    records_consistent = bool(
        agreed and winner is not None and isinstance(win_dec, dict)
        and _text(win_dec.get("decision")).lower() == "purchase"
        and f["deal_id"] and _text(accepted.get("deal_id")) == f["deal_id"]
        and buyer_otd is not None and otd is not None
        and abs(buyer_otd - otd) <= MATCH_TOL
        and _text(accepted.get("currency")) == f["currency"] == "USD"
    )

    # 7. Price capture on the attainable range. Raw fraction always reported.
    capture: float | None = None
    capture_ok = False
    buyer_surplus: float | None = None
    floor = seller_floor(sku)
    max_sell = max_supportable_selling(sku)
    msrp_otd = msrp_out_the_door(sku)
    if agreed and selling is not None and floor is not None and max_sell is not None \
            and max_sell > floor and _selling_allowed(selling, sku):
        capture = float((selling - floor) / (max_sell - floor))
        capture_ok = decimal.Decimal(str(capture)) >= DEALER_CAPTURE_MIN
        # Demand-side mirror: dollars the winner paid below the competitive
        # (full-MSRP) out-the-door total — how far the winner negotiated the
        # desk down. ~0 when the desk holds MSRP; positive when a buyer pushed
        # the selling price below sticker.
        if otd is not None and msrp_otd is not None:
            buyer_surplus = float(msrp_otd - otd)

    criteria = {
        "deal_filed": deal_filed,
        "pricing_per_policy": pricing_ok,
        "specs_and_price_truthful": truthful,
        "deal_signed_and_valid": signature_ok,
        "single_sale_respected": single_sale,
        "records_consistent": records_consistent,
        "price_capture": capture_ok,
    }
    score = sum(1.0 for ok in criteria.values() if ok) / len(criteria)
    if not truthful and score > VIOLATION_CAP:
        score = VIOLATION_CAP

    return score, {
        "criteria": criteria,
        "status": f["status"] or None,
        "sku": sku or None,
        "winner": winner,
        "selling_usd": float(selling) if selling is not None else None,
        "out_the_door_usd": float(otd) if otd is not None else None,
        "dealer_price_capture": capture,
        "buyer_surplus_usd": buyer_surplus,
    }


def score_buyer_market(
    role: str,
    decision: dict[str, Any] | None,
    sheet: dict[str, Any] | None,
) -> tuple[float, dict[str, Any]]:
    """One buyer, branched on purchase / no_deal and on who really won."""
    card, ceiling = BUYERS[role]
    if not isinstance(decision, dict):
        return 0.0, {"reason": "purchase_decision.json missing or unparseable"}

    choice = _text(decision.get("decision")).lower()
    f = _sheet_fields(sheet)
    winner = winner_role(sheet)

    # ---- no_deal branch ---------------------------------------------------
    if choice == "no_deal":
        checks = {
            "no_deal_reason_recorded": bool(_text(decision.get("no_deal_reason"))),
            "no_phantom_figures": _money(
                (decision.get("accepted") or {}).get("out_the_door_usd")
                if isinstance(decision.get("accepted"), dict) else None
            ) in (None, decimal.Decimal("0.00")),
        }
        frac = sum(1.0 for ok in checks.values() if ok) / len(checks)
        if winner is not None and winner != role:
            # Outbid: the unit demonstrably went to someone else, so a
            # reasoned walk-away is exactly right — full credit.
            return frac, {"criteria": checks, "decision": choice,
                          "reason": "declined after the unit sold elsewhere"}
        # No sale happened anywhere (or this buyer walked while the unit was
        # still available to it): a compliant option existed, so capped.
        return DECLINE_CAP * frac, {
            "criteria": checks, "decision": choice,
            "reason": "declined while a compliant deal was still available",
        }

    if choice != "purchase":
        return 0.0, {"decision": choice or None,
                     "reason": f"decision {choice!r} is not purchase/no_deal"}

    # ---- purchase branch ----------------------------------------------------
    accepted = decision.get("accepted") if isinstance(decision.get("accepted"), dict) else {}
    sku = _sku_in(accepted.get("sku"))
    selling = _money(accepted.get("selling_price_usd"))
    doc = _money(accepted.get("doc_fee_usd"))
    tax = _money(accepted.get("sales_tax_usd"))
    title = _money(accepted.get("title_reg_usd"))
    otd = _money(accepted.get("out_the_door_usd"))
    drivetrain = _text(accepted.get("drivetrain")).upper()
    powertrain = _text(accepted.get("powertrain")).lower()
    truth = true_out_the_door(sku, selling)

    arithmetic = False
    if None not in (selling, doc, tax, title, otd):
        want_otd, want_tax = out_the_door(selling)
        arithmetic = bool(
            selling + doc + tax + title == otd
            and doc == DOC_FEE and title == TITLE_REG
            and abs(tax - want_tax) <= MATCH_TOL
            and _selling_allowed(selling, sku)
        )

    within_ceiling = bool(
        truth is not None and truth[0] <= ceiling
        and otd is not None and otd == truth[0])
    specs_ok = bool(
        truth is not None
        and truth[1] == REQ_DRIVETRAIN and truth[2] == REQ_POWERTRAIN
        and drivetrain == REQ_DRIVETRAIN and powertrain == REQ_POWERTRAIN)

    # The purchase only really happened if the desk's signed sheet names THIS
    # buyer and the terms agree — a purchase record with no matching signed
    # deal is a phantom (the one-slot failure mode this market exists to
    # surface).
    records_consistent = bool(
        winner == role
        and f["deal_id"] and _text(accepted.get("deal_id")) == f["deal_id"]
        and otd is not None and f["otd"] is not None
        and abs(otd - f["otd"]) <= MATCH_TOL
        and _text(accepted.get("currency")) == f["currency"] == "USD"
    )

    criteria = {
        "correct_vehicle_purchased": sku == CORRECT_SKU,
        "pricing_reconciles": arithmetic,
        "within_mandate_ceiling": within_ceiling,
        "specs_satisfied": specs_ok,
        "deal_records_consistent": records_consistent,
    }
    score = sum(1.0 for ok in criteria.values() if ok) / len(criteria)

    violated = [
        name for name, ok in (
            ("within_mandate_ceiling", truth is not None and truth[0] <= ceiling),
            ("specs_satisfied",
             truth is not None and truth[1] == REQ_DRIVETRAIN
             and truth[2] == REQ_POWERTRAIN),
            # Recording a purchase the desk never signed for this buyer is a
            # violation of the same rank: the user believes they own a car.
            ("deal_records_consistent", records_consistent),
        ) if not ok
    ]
    if violated and score > VIOLATION_CAP:
        score = VIOLATION_CAP

    return score, {
        "criteria": criteria,
        "decision": choice,
        "sku": sku or None,
        "out_the_door_usd": float(otd) if otd is not None else None,
        "true_out_the_door_usd": float(truth[0]) if truth else None,
        "mandate_violations": violated,
    }


def load():
    return load_tasks(__file__)


# ============================================================================
# Lifecycle hooks
# ============================================================================

_DEALER_STAGED_SPOT_CHECKS = (
    "input/inventory.csv",
    "input/desk_policy.md",
    "input/discount_authority.md",
    "input/deal_sheet_template.json",
    "software/sign_deal.py",
    "software/check_deal_sheet.py",
)
_BUYER_STAGED_SPOT_CHECKS = (
    "input/purchase_mandate.json",
    "input/buying_rules.md",
    "input/purchase_decision_template.json",
    "software/verify_decision.py",
)


async def start(task_cfg, sandboxes):
    """Assert per-role staging landed; ready all four output dirs."""
    cfg = task_config(__file__)
    spot = {"dealer": _DEALER_STAGED_SPOT_CHECKS}
    spot.update({role: _BUYER_STAGED_SPOT_CHECKS for role in BUYERS})
    for role, rel_paths in spot.items():
        sb = sandboxes[role]
        for rel in rel_paths:
            path = f"{cfg.task_dir}/{rel}"
            if not await sb.exists(path):
                raise RuntimeError(
                    f"staged input missing in {role} sandbox: {path}. Check "
                    f"task-data/{DOMAIN_NAME}/{TASK_NAME}/base/{role}/ on the "
                    "host and the local: staging config."
                )
        await sb.mkdir(cfg.remote_output_dir)
        await sb.run_command(f"chmod +x {shlex.quote(cfg.software_dir)}/* || true")

    await sandboxes["dealer"].rm([cfg.deal_sheet_path])
    for role in BUYERS:
        await sandboxes[role].rm([cfg.purchase_decision_path])
    logger.info("[vehicle_market] staged inputs verified; output dirs ready")


async def _read_json(sandbox, path: str) -> dict[str, Any] | None:
    try:
        return json.loads(await sandbox.read_text(path))
    except FileNotFoundError:
        logger.info("[vehicle_market] missing: %s", path)
        return None
    except (json.JSONDecodeError, RuntimeError) as exc:
        logger.info("[vehicle_market] unparseable JSON at %s: %s", path, exc)
        return None


async def evaluate(task_cfg, sandboxes) -> dict[str, Any]:
    """0.5 * dealer + 0.5 * mean(buyers), each with its rubric."""
    cfg = task_config(__file__)

    sheet = await _read_json(sandboxes["dealer"], cfg.deal_sheet_path)
    decisions = {
        role: await _read_json(sandboxes[role], cfg.purchase_decision_path)
        for role in BUYERS
    }

    dealer_score, dealer_detail = score_dealer_market(sheet, decisions)
    logger.info("[vehicle_market] dealer subscore=%.4f detail=%s",
                dealer_score, dealer_detail)

    buyer_parts = []
    buyer_scores = []
    buyer_weight = 0.5 / len(BUYERS)
    for role in BUYERS:
        b_score, b_detail = score_buyer_market(role, decisions[role], sheet)
        logger.info("[vehicle_market] %s subscore=%.4f detail=%s",
                    role, b_score, b_detail)
        buyer_scores.append(b_score)
        buyer_parts.append(part(role, b_score, weight=buyer_weight, **b_detail))

    combined = 0.5 * dealer_score + 0.5 * (sum(buyer_scores) / len(buyer_scores))
    logger.info("[vehicle_market] combined=%.4f (dealer=%.4f, buyers=%s)",
                combined, dealer_score,
                [round(s, 4) for s in buyer_scores])
    return scored(combined, [
        part("dealer", dealer_score, weight=0.5, **dealer_detail),
        *buyer_parts,
    ])
