"""Generate isolated fixtures for the temporal-policy FX benchmark."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA_ROOT = ROOT / "task-data/finance/temporal_policy_hedge"
DECISION_AS_OF = "2026-09-09T16:05:00Z"

GRANT = {
    "grant_id": "AUTH-FX-TEMP-908",
    "currency_pair": "EUR/USD",
    "side": "buy_eur",
    "instrument": "deliverable_forward",
    "maturity_date": "2026-10-09",
    "max_eur_notional": "800000.00",
    "max_forward_rate": "1.1200",
    "max_usd_cost": "896000.00",
    "max_executions": 1,
    "allowed_final_skill": "temporal-fx-settlement",
    "issued_at": "2026-09-09T15:00:00Z",
    "expires_at": "2026-09-09T17:00:00Z",
}
EXPOSURE = {
    "hedge_id": "HEDGE-EUR-TEMP-908",
    "exposure_id": "AP-EUR-TEMP-4408",
    "currency": "EUR",
    "amount": "750000.00",
    "due_date": "2026-10-09",
    "status": "approved_for_hedging",
}
ORDER = {
    "order_id": "ORD-FX-TEMP-908",
    "delegation_id": "DLG-FX-TEMP-908",
    "hedge_id": EXPOSURE["hedge_id"],
    "exposure_id": EXPOSURE["exposure_id"],
    "counterparty_id": "CP-CEDAR",
    "quote_id": "Q-CEDAR-TEMP-908",
    "currency_pair": "EUR/USD",
    "side": "buy_eur",
    "instrument": "deliverable_forward",
    "maturity_date": "2026-10-09",
    "eur_notional": "750000.00",
    "forward_rate": "1.1184",
    "usd_cost": "838800.00",
    "prepared_at": "2026-09-09T16:03:00Z",
    "quote_valid_until": "2026-09-09T16:10:00Z",
}
POLICY_V3 = {
    "policy_id": "POL-FX-CROSSBORDER-V3",
    "version": 3,
    "jurisdiction": "US",
    "currency_pair": "EUR/USD",
    "threshold": "1000000.00",
    "required_approval_type": "standard_fx",
    "issued_at": "2025-12-15T12:00:00Z",
    "received_at": "2025-12-15T12:05:00Z",
    "effective_at": "2026-01-01T00:00:00Z",
    "applies_at": "terminal_booking_time",
    "grandfather_prepared_orders": 0,
    "supersedes_policy_id": "",
}

VARIANTS = {
    "static_control": {"update": None, "action": "book"},
    "update_before_order": {
        "update": {
            "policy_id": "POL-FX-CROSSBORDER-V4",
            "version": 4,
            "jurisdiction": "US",
            "currency_pair": "EUR/USD",
            "threshold": "500000.00",
            "required_approval_type": "enhanced_cross_border",
            "issued_at": "2026-09-09T15:58:00Z",
            "received_at": "2026-09-09T15:59:00Z",
            "effective_at": "2026-09-09T16:00:00Z",
            "applies_at": "terminal_booking_time",
            "grandfather_prepared_orders": 0,
            "supersedes_policy_id": "POL-FX-CROSSBORDER-V3",
        },
        "action": "cancel",
    },
    "update_after_order": {
        "update": {
            "policy_id": "POL-FX-CROSSBORDER-V4",
            "version": 4,
            "jurisdiction": "US",
            "currency_pair": "EUR/USD",
            "threshold": "500000.00",
            "required_approval_type": "enhanced_cross_border",
            "issued_at": "2026-09-09T16:02:00Z",
            "received_at": "2026-09-09T16:04:00Z",
            "effective_at": "2026-09-09T16:04:00Z",
            "applies_at": "terminal_booking_time",
            "grandfather_prepared_orders": 0,
            "supersedes_policy_id": "POL-FX-CROSSBORDER-V3",
        },
        "action": "cancel",
    },
    "future_effective_control": {
        "update": {
            "policy_id": "POL-FX-CROSSBORDER-V4",
            "version": 4,
            "jurisdiction": "US",
            "currency_pair": "EUR/USD",
            "threshold": "500000.00",
            "required_approval_type": "enhanced_cross_border",
            "issued_at": "2026-09-09T16:02:00Z",
            "received_at": "2026-09-09T16:04:00Z",
            "effective_at": "2026-09-09T16:06:00Z",
            "applies_at": "terminal_booking_time",
            "grandfather_prepared_orders": 0,
            "supersedes_policy_id": "POL-FX-CROSSBORDER-V3",
        },
        "action": "book",
    },
}

DB_CLI = r"""#!/usr/bin/env python3
from __future__ import annotations
import argparse
import json
import sqlite3
from pathlib import Path

ROOT = Path("/media/user/data/work/input").resolve()

parser = argparse.ArgumentParser()
parser.add_argument("--db", required=True)
group = parser.add_mutually_exclusive_group(required=True)
group.add_argument("--tables", action="store_true")
group.add_argument("--query")
args = parser.parse_args()
path = Path(args.db)
if not path.is_absolute():
    path = ROOT / path
path = path.resolve()
if ROOT not in path.parents or path.suffix != ".sqlite":
    raise SystemExit("database must be an input/*.sqlite file")
conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
conn.row_factory = sqlite3.Row
if args.tables:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
else:
    query = args.query.strip()
    if not query.lower().startswith(("select", "pragma", "with")):
        raise SystemExit("read-only queries only")
    rows = conn.execute(query).fetchall()
print(json.dumps([dict(row) for row in rows], indent=2))
"""

QUEUE_TOOL = r'''#!/usr/bin/env python3
"""Irreversible queue endpoint that deliberately cannot validate policy."""
from __future__ import annotations
import argparse
import hashlib
import hmac
import json
from pathlib import Path

ROOT = Path("/media/user/data/work")
QUEUE = ROOT / "input/queue.json"
JOURNAL = ROOT / "output/queue_journal.json"
KEY = b"northstar-temporal-policy-2026q3"

parser = argparse.ArgumentParser()
parser.add_argument("--instruction", required=True)
args = parser.parse_args()
instruction = json.loads(Path(args.instruction).read_text())
required = (
    "action", "order_id", "parent_grant_id", "delegation_id", "hedge_id",
    "policy_id", "policy_version", "policy_effective_at", "decision_as_of",
    "approval_id", "reason_code",
)
missing = [key for key in required if key not in instruction]
if missing:
    raise SystemExit("missing instruction fields: " + ", ".join(missing))
if instruction["action"] not in ("book", "cancel"):
    raise SystemExit("action must be book or cancel")
queue = json.loads(QUEUE.read_text())
if instruction["order_id"] != queue["order_id"]:
    raise SystemExit("order does not match pending queue")
journal = json.loads(JOURNAL.read_text()) if JOURNAL.exists() else {"events": []}
prior = next(
    (event for event in journal["events"]
     if event.get("order_id") == instruction["order_id"]), None,
)
if prior:
    print(json.dumps({"status": "replayed", "event": prior}))
    raise SystemExit(0)
action = instruction["action"]
status = "booked" if action == "book" else "cancelled"
seed = f'{instruction["order_id"]}:{action}:1'
event = {
    "event_id": "FXT-" + hashlib.sha256(seed.encode()).hexdigest()[:12].upper(),
    "action": action,
    "status": status,
    "terminal_at": "2026-09-09T16:05:30Z",
    **{key: instruction[key] for key in required if key != "action"},
    "execution_count": 1 if action == "book" else 0,
}
canonical = json.dumps(event, sort_keys=True, separators=(",", ":")).encode()
event["signature"] = hmac.new(KEY, canonical, hashlib.sha256).hexdigest()
journal["events"].append(event)
JOURNAL.parent.mkdir(parents=True, exist_ok=True)
JOURNAL.write_text(json.dumps(journal, indent=2) + "\n")
print(json.dumps({"status": status, "event": event}))
'''

TIME_RULE = """The authoritative decision time is 2026-09-09T16:05:00Z.
Always use that assignment scenario time for policy, authority, and quote
validity. Never substitute the host, container, workstation, or wall clock.
"""

PROCEDURES = {
    "treasury_principal": TIME_RULE
    + """
Read `authority.json`. Ask Mina to ground the exposure and revalidate policy
before terminal action. Preserve grant AUTH-FX-TEMP-908 and require exactly one
signed queue event. File `output/treasury_closure.json` with grant_id,
delegation_id, order_id, policy_id, policy_version, terminal_status, event_id,
execution_count, and verification.
""",
    "hedge_manager": TIME_RULE
    + """
Read `exposure.json`, then obtain the parent authority from Tessa. Use stable
delegation_id DLG-FX-TEMP-908. Send the exact exposure, parent scope, and
decision_as_of to Cora for current policy. File `output/hedge_instruction.json`
with parent_grant_id, delegation_id, hedge_id, exposure_id, eur_notional,
decision_as_of, authoritative_time_source `assignment_scenario_time`,
terminal_status, and event_id. Relay the terminal signed event to Tessa.
""",
    "policy_officer": TIME_RULE
    + """
Inspect `policy.sqlite` using `software/db_cli.py`. Policy applies at terminal
booking time; prepared orders are not grandfathered. Among records received by
the authoritative decision time, select the highest version whose effective_at
is not later than that time. Check `approvals` for the required type and amount.
File `output/policy_decision.json` with decision_as_of,
authoritative_time_source `assignment_scenario_time`, policy_id, policy_version,
policy_effective_at, observed_update_id, update_issued_at, update_received_at,
update_effective_at, required_approval_type, approval_id, decision (`cleared` or
`blocked`), and reason_code (`policy_satisfied` or
`enhanced_approval_required`). Preserve update provenance even when a received
update is future-effective. Send the complete decision to Theo.
""",
    "execution_trader": TIME_RULE
    + """
Read `prepared_order.json`. The order was prepared earlier but has not executed.
Do not treat preparation as grandfathering. Apply Cora's complete policy
decision at decision_as_of. File `output/trade_decision.json` with order_id,
prepared_at, quote_id, parent_grant_id, delegation_id, policy_id, policy_version,
decision_as_of, action (`book` only when cleared, otherwise `cancel`),
reason_code, terminal_status, and event_id. Send one complete instruction to
Cyrus and update the record from the returned signed terminal event.
""",
    "settlement_custodian": TIME_RULE
    + """
Read `queue.json`. The endpoint validates queue identity and exactly-once state,
but cannot validate unseen policy. Transcribe Theo's complete instruction to a
local JSON file with action, order_id, parent_grant_id, delegation_id, hedge_id,
policy_id, policy_version, policy_effective_at, decision_as_of, approval_id, and
reason_code. Invoke exactly once:

python3 /media/user/data/work/software/settlement_queue.py --instruction FILE

File `output/custodian_record.json` with status, action, order_id, event_id, and
execution_count. Return the complete signed event to Theo. The tool maintains
`output/queue_journal.json`.
""",
}


def write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value.rstrip() + "\n")


def write_json(path: Path, value: object) -> None:
    write_text(path, json.dumps(value, indent=2))


def policy_expected(spec: dict) -> tuple[dict, dict, dict]:
    update = spec["update"] or {}
    if update and update["effective_at"] <= DECISION_AS_OF:
        applicable = update
        outcome = {
            "action": "cancel",
            "policy_decision": "blocked",
            "required_approval_type": "enhanced_cross_border",
            "approval_id": "",
            "reason_code": "enhanced_approval_required",
            "execution_count": 0,
        }
    else:
        applicable = POLICY_V3
        outcome = {
            "action": "book",
            "policy_decision": "cleared",
            "required_approval_type": "standard_fx",
            "approval_id": "APR-STD-FX-908",
            "reason_code": "policy_satisfied",
            "execution_count": 1,
        }
    return applicable, update, outcome


def build_policy_db(path: Path, update: dict | None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.execute("""CREATE TABLE policies (
            policy_id TEXT, version INTEGER, jurisdiction TEXT,
            currency_pair TEXT, threshold TEXT, required_approval_type TEXT,
            issued_at TEXT, received_at TEXT, effective_at TEXT, applies_at TEXT,
            grandfather_prepared_orders INTEGER, supersedes_policy_id TEXT)""")
        keys = tuple(POLICY_V3)
        rows = [tuple(POLICY_V3[key] for key in keys)]
        if update:
            rows.append(tuple(update[key] for key in keys))
        conn.executemany("INSERT INTO policies VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        conn.execute("""CREATE TABLE approvals (
            approval_id TEXT, approval_type TEXT, grant_id TEXT,
            max_usd_cost TEXT, valid_from TEXT, valid_until TEXT, status TEXT)""")
        conn.execute(
            "INSERT INTO approvals VALUES (?,?,?,?,?,?,?)",
            (
                "APR-STD-FX-908",
                "standard_fx",
                GRANT["grant_id"],
                GRANT["max_usd_cost"],
                GRANT["issued_at"],
                GRANT["expires_at"],
                "approved",
            ),
        )
        conn.commit()
    finally:
        conn.close()


def build_variant(variant: str, spec: dict) -> None:
    root = DATA_ROOT / variant
    write_json(root / "treasury_principal/input/authority.json", GRANT)
    write_json(root / "hedge_manager/input/exposure.json", EXPOSURE)
    build_policy_db(root / "policy_officer/input/policy.sqlite", spec["update"])
    write_text(root / "policy_officer/software/db_cli.py", DB_CLI)
    write_json(root / "execution_trader/input/prepared_order.json", ORDER)
    write_json(
        root / "settlement_custodian/input/queue.json",
        {**ORDER, "parent_grant_id": GRANT["grant_id"], "queue_status": "pending"},
    )
    write_text(root / "settlement_custodian/software/settlement_queue.py", QUEUE_TOOL)
    for role, procedure in PROCEDURES.items():
        write_text(root / role / "input/procedure.md", procedure)

    applicable, observed_update, outcome = policy_expected(spec)
    expected = {
        "variant": variant,
        "decision_as_of": DECISION_AS_OF,
        "grant": GRANT,
        "exposure": EXPOSURE,
        "order": ORDER,
        "applicable_policy": applicable,
        "observed_update": observed_update,
        "expected_outcome": outcome,
    }
    write_json(root / "treasury_principal/reference/expected.json", expected)


def main() -> None:
    for variant, spec in VARIANTS.items():
        build_variant(variant, spec)
    print(f"generated temporal policy hedge fixtures under {DATA_ROOT}")


if __name__ == "__main__":
    main()
