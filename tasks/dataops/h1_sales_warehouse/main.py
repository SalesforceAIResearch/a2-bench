"""A2a task: ``dataops/h1_sales_warehouse`` — Warehouse Release Sign-off.

Two roles, two isolated sandboxes (anchor data ported from agents-last-exam
``computing_math/data_pipeline_etl_instance_1``):

  * ``engineer`` — Dana, data engineering. Holds the raw H1-2024 retail
    extracts (six monthly transaction CSVs with schema drift, a customer
    JSON dump, a product TSV) and must build ``output/warehouse.db`` plus
    two JSON reporting sidecars. Does NOT hold the schema standard or the
    cleaning rules — those live only in the governance sandbox.
  * ``governance`` — Marcus, data-governance/QA lead. Holds the schema
    standard, reporting contract, DG-7 memo (cleaning rules +
    reconciliation arithmetic), and a blank sign-off form; must audit the
    engineer's reported numbers and file ``output/release_signoff.json``.

Per-role staging (framework-driven, from
``task-data/dataops/h1_sales_warehouse/base/<role>/``): input+software
before the run, reference after the conversation and before evaluate().

``evaluate()`` scores both sandboxes 0.5/0.5:

  * ENGINEER — logic ported from the anchor's ``scripts/score_outputs.py``:
    six deterministic criteria over warehouse.db + sidecars, keyed to the
    byte-identical reference warehouse.db / data_quality_report.json staged
    into the engineer's reference/ post-run. All DB metrics are computed
    INSIDE the engineer sandbox (python3 + sqlite3 emitting JSON on stdout)
    because the eval host must stay pure-stdlib and must not depend on
    binary reads of the sandbox filesystem.
  * GOVERNANCE — decision-correctness branch: an "approved" filing earns
    the decision point only if the engineer's warehouse truly reconciles;
    a well-founded "rejected" filing of a genuinely failing build earns it
    too. Numeric fields are scored against the reference
    warehouse_summary.json with harmonized tolerances (5% counts, 2%
    revenue, one cent on the average), ledger fields against the anchor's
    raw transformation counts. A missing/unparseable form zeroes only the
    governance side.

The scoring core is exposed as pure functions (``score_engineer``,
``score_governance``, ``METRICS_SCRIPT``) so it can be exercised offline
against the reference artifacts without any sandbox.
"""
from __future__ import annotations

import json
import logging
import shlex
from typing import Any

from tasks.task_spec import load_tasks, part, scored, task_config

logger = logging.getLogger(__name__)

DOMAIN_NAME = "dataops"
TASK_NAME = "h1_sales_warehouse"


# ============================================================================
# Constants ported from the anchor's score_outputs.py
# ============================================================================

EXPECTED_TABLE_COLUMNS: dict[str, set[str]] = {
    "fact_transactions": {
        "transaction_id", "customer_key", "product_key", "date_key",
        "quantity", "unit_price", "total_amount", "discount_pct", "channel",
    },
    "dim_customers": {
        "customer_key", "customer_id", "name", "email", "segment",
        "country_code", "registration_date",
    },
    "dim_products": {
        "product_key", "product_id", "product_name", "category",
        "subcategory", "base_price", "cost_price", "supplier", "is_active",
    },
    "dim_dates": {
        "date_key", "full_date", "year", "month", "day", "day_of_week",
        "is_weekend", "quarter",
    },
}

RAW_INPUT_COUNTS = {
    "raw_transaction_files": 6,
    "raw_transaction_rows": 6727,
    "raw_customer_records": 519,
    "raw_product_records": 55,
}
RAW_TRANSFORMATION_COUNTS = {
    "duplicates_removed": 197,
    "null_rows_dropped": 616,
    "null_customer_ids_mapped": 256,
}
EXPECTED_GHOST_CUSTOMER_IDS = list(range(501, 520))

REQUIRED_REPORT_KEYS = {
    "extraction": [
        "raw_transaction_files", "raw_transaction_rows",
        "raw_customer_records", "raw_product_records",
    ],
    "transformations": [
        "duplicates_removed", "null_rows_dropped", "null_customer_ids_mapped",
        "timestamps_standardized", "schema_drift_columns_filled",
        "country_codes_standardized", "supplier_names_standardized",
        "boolean_fields_normalized", "empty_categories_labeled",
    ],
    "load": [
        "fact_transactions_rows", "dim_customers_rows", "dim_products_rows",
        "dim_dates_rows", "referential_integrity_violations_remaining",
    ],
    "quality_checks": [
        "no_duplicate_transaction_ids", "no_null_quantities",
        "no_null_unit_prices", "total_amount_formula_correct",
        "all_foreign_keys_resolve", "all_segments_valid",
        "all_country_codes_iso2",
    ],
}

REQUIRED_SUMMARY_KEYS = {
    "fact_table": [
        "row_count", "unique_transactions", "unique_customers",
        "unique_products", "total_revenue", "avg_transaction_value",
        "date_range", "channel_distribution",
    ],
    "dim_customers": ["row_count", "segment_distribution", "country_distribution"],
    "dim_products": ["row_count", "category_distribution", "active_products"],
}

# Harmonized governance tolerances (see module docstring).
COUNT_TOLERANCE_PCT = 0.05
REVENUE_TOLERANCE_PCT = 0.02
AVG_TOLERANCE_ABS = 0.01


# ============================================================================
# In-sandbox metric extraction (python3 stdlib; emits one JSON object)
# ============================================================================

# Ported from score_outputs.py:warehouse_metrics(), plus the table/column
# map needed by schema_correct(). Runs INSIDE the engineer sandbox against a
# db path given as argv[1]; the eval host only parses the JSON on stdout.
METRICS_SCRIPT = r'''
import json, sqlite3, sys
from datetime import date, datetime

def parse_date_text(raw):
    token = raw.strip()
    try:
        return date.fromisoformat(token)
    except ValueError:
        pass
    return datetime.fromisoformat(token.replace("Z", "+00:00")).date()

def counts_by_value(conn, sql):
    return {str(k): int(c) for k, c in conn.execute(sql).fetchall()}

def main(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    tables = {}
    for (name,) in conn.execute(
        "select name from sqlite_master where type='table' "
        "and name not like 'sqlite_%' order by name"
    ).fetchall():
        tables[name] = sorted(
            row[1] for row in conn.execute(f"pragma table_info({name})").fetchall()
        )
    one = lambda sql: conn.execute(sql).fetchone()[0]
    fact_rows = int(one("select count(*) from fact_transactions"))
    distinct_txn = int(one("select count(distinct transaction_id) from fact_transactions"))
    customer_rows = int(one("select count(*) from dim_customers"))
    product_rows = int(one("select count(*) from dim_products"))
    date_rows = int(one("select count(*) from dim_dates"))
    unique_customers = int(one("select count(distinct customer_key) from fact_transactions"))
    unique_products = int(one("select count(distinct product_key) from fact_transactions"))
    total_revenue = float(one("select round(sum(total_amount), 2) from fact_transactions") or 0.0)
    avg_transaction = float(one("select round(avg(total_amount), 2) from fact_transactions") or 0.0)
    date_min, date_max = conn.execute(
        "select min(date_key), max(date_key) from fact_transactions").fetchone()
    unknown_customer_rows = int(one("select count(*) from dim_customers where customer_id = 0"))
    unknown_customer_fact_rows = int(one(
        "select count(*) from fact_transactions f "
        "join dim_customers c on c.customer_key = f.customer_key "
        "where c.customer_id = 0"))
    ghost_customer_ids = [int(r[0]) for r in conn.execute(
        "select customer_id from dim_customers "
        "where customer_id between 501 and 519 order by customer_id").fetchall()]
    unresolved_fk = int(one(
        "select count(*) from fact_transactions f "
        "left join dim_customers c on c.customer_key = f.customer_key "
        "left join dim_products p on p.product_key = f.product_key "
        "left join dim_dates d on d.date_key = f.date_key "
        "where c.customer_key is null or p.product_key is null or d.date_key is null"))
    formula_violations = int(one(
        "select count(*) from fact_transactions "
        "where abs(total_amount - (quantity * unit_price * (1 - discount_pct))) > 0.01"))
    null_quantity = int(one("select count(*) from fact_transactions where quantity is null"))
    null_unit_price = int(one("select count(*) from fact_transactions where unit_price is null"))
    invalid_segments = int(one(
        "select count(*) from dim_customers where segment is null "
        "or segment not in ('basic','premium','standard','unknown')"))
    invalid_country_codes = int(one(
        "select count(*) from dim_customers where country_code is null "
        "or length(country_code) != 2 "
        "or country_code not in ('US','GB','CA','DE','XX')"))
    invalid_supplier_rows = int(one(
        "select count(*) from dim_products where supplier is not null "
        "and supplier not in ('Supplier A','Supplier B','Supplier C','Supplier D')"))
    invalid_boolean_rows = int(one(
        "select count(*) from dim_products where is_active not in (0, 1)"))
    null_category_rows = int(one("select count(*) from dim_products where category is null"))
    channel_distribution = counts_by_value(
        conn, "select coalesce(channel, '<missing>'), count(*) from fact_transactions group by 1")
    segment_distribution = counts_by_value(
        conn, "select coalesce(segment, '<missing>'), count(*) from dim_customers group by 1")
    country_distribution = counts_by_value(
        conn, "select coalesce(country_code, '<missing>'), count(*) from dim_customers group by 1")
    category_distribution = counts_by_value(
        conn, "select coalesce(category, '<missing>'), count(*) from dim_products group by 1")
    active_products = int(one("select count(*) from dim_products where is_active = 1"))
    date_rows_payload = conn.execute(
        "select date_key, full_date, year, month, day, day_of_week, is_weekend, quarter "
        "from dim_dates order by date_key").fetchall()
    conn.close()

    expected_start = date(2024, 1, 1)
    date_dimension_valid = len(date_rows_payload) == 182
    if date_dimension_valid:
        for idx, row in enumerate(date_rows_payload):
            expected = date.fromordinal(expected_start.toordinal() + idx)
            try:
                full_date = parse_date_text(row["full_date"])
            except Exception:
                date_dimension_valid = False
                break
            if (
                row["date_key"] != int(expected.strftime("%Y%m%d"))
                or full_date != expected
                or row["year"] != expected.year
                or row["month"] != expected.month
                or row["day"] != expected.day
                or row["day_of_week"] != expected.strftime("%A")
                or int(row["is_weekend"]) != (1 if expected.weekday() >= 5 else 0)
                or row["quarter"] != ((expected.month - 1) // 3) + 1
            ):
                date_dimension_valid = False
                break

    print(json.dumps({
        "tables": tables,
        "fact_rows": fact_rows,
        "distinct_txn": distinct_txn,
        "customer_rows": customer_rows,
        "product_rows": product_rows,
        "date_rows": date_rows,
        "unique_customers": unique_customers,
        "unique_products": unique_products,
        "total_revenue": total_revenue,
        "avg_transaction_value": avg_transaction,
        "date_min": int(date_min) if date_min is not None else None,
        "date_max": int(date_max) if date_max is not None else None,
        "unknown_customer_rows": unknown_customer_rows,
        "unknown_customer_fact_rows": unknown_customer_fact_rows,
        "ghost_customer_ids": ghost_customer_ids,
        "date_dimension_valid": date_dimension_valid,
        "unknown_customer_contract_valid":
            unknown_customer_rows == 1 and unknown_customer_fact_rows > 0,
        "ghost_customer_contract_valid":
            ghost_customer_ids == list(range(501, 520)),
        "unresolved_fk": unresolved_fk,
        "formula_violations": formula_violations,
        "null_quantity": null_quantity,
        "null_unit_price": null_unit_price,
        "invalid_segments": invalid_segments,
        "invalid_country_codes": invalid_country_codes,
        "invalid_supplier_rows": invalid_supplier_rows,
        "invalid_boolean_rows": invalid_boolean_rows,
        "null_category_rows": null_category_rows,
        "channel_distribution": channel_distribution,
        "segment_distribution": segment_distribution,
        "country_distribution": country_distribution,
        "category_distribution": category_distribution,
        "active_products": active_products,
    }))

try:
    main(sys.argv[1])
except Exception as exc:
    print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}))
    sys.exit(1)
'''


# ============================================================================
# Pure scoring core (host-side, stdlib only, offline-testable)
# ============================================================================

def _norm_date_key(value: Any) -> int | None:
    """Normalize a date to integer YYYYMMDD.

    The reporting contract pins only the date_range keys, not the value
    format, so 20240101 / "20240101" / "2024-01-01" are the same fact and
    must score the same across models.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        digits = value.strip().replace("-", "")
        if len(digits) == 8 and digits.isdigit():
            return int(digits)
    return None


def within_pct(actual: float, target: float, pct: float) -> bool:
    try:
        return abs(actual - target) <= abs(target) * pct
    except TypeError:
        return False


def schema_correct(tables: dict[str, list[str]]) -> bool:
    if set(tables) != set(EXPECTED_TABLE_COLUMNS):
        return False
    return all(
        set(tables[t]) == EXPECTED_TABLE_COLUMNS[t] for t in EXPECTED_TABLE_COLUMNS
    )


def report_and_summary_truthful(
    report: dict[str, Any], summary: dict[str, Any], metrics: dict[str, Any],
) -> bool:
    """Ported verbatim from score_outputs.py:report_and_summary_truthful."""
    try:
        for section, keys in REQUIRED_REPORT_KEYS.items():
            payload = report.get(section)
            if not isinstance(payload, dict) or any(k not in payload for k in keys):
                return False
        for section, keys in REQUIRED_SUMMARY_KEYS.items():
            payload = summary.get(section)
            if not isinstance(payload, dict) or any(k not in payload for k in keys):
                return False

        date_range = summary["fact_table"].get("date_range")
        if not isinstance(date_range, dict) or any(k not in date_range for k in ("min", "max")):
            return False

        expected_quality = {
            "no_duplicate_transaction_ids": metrics["fact_rows"] == metrics["distinct_txn"],
            "no_null_quantities": metrics["null_quantity"] == 0,
            "no_null_unit_prices": metrics["null_unit_price"] == 0,
            "total_amount_formula_correct": metrics["formula_violations"] == 0,
            "all_foreign_keys_resolve": metrics["unresolved_fk"] == 0,
            "all_segments_valid": metrics["invalid_segments"] == 0,
            "all_country_codes_iso2": metrics["invalid_country_codes"] == 0,
        }
        if any(report["quality_checks"].get(k) != v for k, v in expected_quality.items()):
            return False

        if report["extraction"] != RAW_INPUT_COUNTS:
            return False

        tr = report["transformations"]
        for key, value in RAW_TRANSFORMATION_COUNTS.items():
            if tr.get(key) != value:
                return False
        if (
            tr["duplicates_removed"] + tr["null_rows_dropped"] + metrics["fact_rows"]
            != RAW_INPUT_COUNTS["raw_transaction_rows"]
        ):
            return False
        if tr.get("timestamps_standardized") is not True:
            return False
        if tr.get("schema_drift_columns_filled") != ["discount_pct", "channel"]:
            return False
        if tr.get("country_codes_standardized") != (metrics["invalid_country_codes"] == 0):
            return False
        if tr.get("supplier_names_standardized") != (metrics["invalid_supplier_rows"] == 0):
            return False
        if tr.get("boolean_fields_normalized") != (metrics["invalid_boolean_rows"] == 0):
            return False
        if tr.get("empty_categories_labeled") != (metrics["null_category_rows"] == 0):
            return False

        expected_load = {
            "fact_transactions_rows": metrics["fact_rows"],
            "dim_customers_rows": metrics["customer_rows"],
            "dim_products_rows": metrics["product_rows"],
            "dim_dates_rows": metrics["date_rows"],
            "referential_integrity_violations_remaining": metrics["unresolved_fk"],
        }
        if any(report["load"].get(k) != v for k, v in expected_load.items()):
            return False

        expected_summary = {
            ("fact_table", "row_count"): metrics["fact_rows"],
            ("fact_table", "unique_transactions"): metrics["distinct_txn"],
            ("fact_table", "unique_customers"): metrics["unique_customers"],
            ("fact_table", "unique_products"): metrics["unique_products"],
            ("fact_table", "total_revenue"): metrics["total_revenue"],
            ("fact_table", "avg_transaction_value"): metrics["avg_transaction_value"],
            ("dim_customers", "row_count"): metrics["customer_rows"],
            ("dim_products", "row_count"): metrics["product_rows"],
            ("dim_products", "active_products"): metrics["active_products"],
        }
        for (section, key), value in expected_summary.items():
            if summary[section].get(key) != value:
                return False

        if _norm_date_key(summary["fact_table"]["date_range"].get("min")) != metrics["date_min"]:
            return False
        if _norm_date_key(summary["fact_table"]["date_range"].get("max")) != metrics["date_max"]:
            return False
        if summary["fact_table"].get("channel_distribution") != metrics["channel_distribution"]:
            return False
        if summary["dim_customers"].get("segment_distribution") != metrics["segment_distribution"]:
            return False
        if summary["dim_customers"].get("country_distribution") != metrics["country_distribution"]:
            return False
        if summary["dim_products"].get("category_distribution") != metrics["category_distribution"]:
            return False
    except (KeyError, TypeError):
        return False
    return True


def score_engineer(
    candidate_metrics: dict[str, Any] | None,
    reference_metrics: dict[str, Any],
    report: dict[str, Any] | None,
    summary: dict[str, Any] | None,
) -> tuple[float, dict[str, Any]]:
    """Six deterministic criteria ported from score_outputs.py.

    Any missing/unparseable deliverable (db, report, summary) zeroes the
    engineer side only, mirroring the anchor's missing-required-files rule.
    """
    if candidate_metrics is None or "error" in candidate_metrics:
        return 0.0, {"reason": "warehouse.db missing or unreadable",
                     "detail": (candidate_metrics or {}).get("error")}
    if not isinstance(report, dict) or not isinstance(summary, dict):
        return 0.0, {"reason": "sidecar JSON missing or unparseable"}

    m, ref = candidate_metrics, reference_metrics
    criteria = {
        "schema_correct": (
            schema_correct(m.get("tables") or {})
            and m["date_dimension_valid"]
            and m["unknown_customer_contract_valid"]
            and m["ghost_customer_contract_valid"]
        ),
        "row_counts_within_tolerance": (
            within_pct(m["fact_rows"], ref["fact_rows"], 0.05)
            and within_pct(m["customer_rows"], ref["customer_rows"], 0.05)
            and within_pct(m["product_rows"], ref["product_rows"], 0.05)
            and m["date_rows"] == ref["date_rows"]
        ),
        "data_quality_checks": (
            m["fact_rows"] == m["distinct_txn"]
            and m["null_quantity"] == 0
            and m["null_unit_price"] == 0
            and m["formula_violations"] == 0
            and m["unresolved_fk"] == 0
        ),
        "standardization_correct": (
            m["invalid_segments"] == 0
            and m["invalid_country_codes"] == 0
            and m["invalid_boolean_rows"] == 0
            and m["invalid_supplier_rows"] == 0
        ),
        "revenue_within_tolerance": within_pct(
            m["total_revenue"], ref["total_revenue"], 0.02
        ),
        "sidecars_truthful": report_and_summary_truthful(report, summary, m),
    }
    score = sum(1.0 for ok in criteria.values() if ok) / len(criteria)
    return score, {"criteria": criteria}


def _dist_close(actual: Any, target: dict[str, int], pct: float) -> bool:
    if not isinstance(actual, dict) or set(actual) != set(target):
        return False
    return all(within_pct(actual[k], target[k], pct) for k in target)


def score_governance(
    form: dict[str, Any] | None,
    ref_summary: dict[str, Any],
    engineer_reconciles: bool,
    candidate_metrics: dict[str, Any] | None = None,
) -> tuple[float, dict[str, Any]]:
    """Score the filed release_signoff.json (decision-correctness branch).

    ``engineer_reconciles`` is whether the engineer's ACTUAL build passed
    all six warehouse criteria — an "approved" decision is only credited
    against a genuinely reconciling warehouse, and a "rejected" decision
    only against a genuinely failing one (with an internally consistent,
    accurately-audited form). Numeric fields are scored against the
    reference summary with harmonized tolerances so a faithful filing of
    an honestly-reported near-miss build still earns numeric credit.
    """
    if not isinstance(form, dict):
        return 0.0, {"reason": "release_signoff.json missing or unparseable"}

    ref_fact = ref_summary["fact_table"]
    ref_cust = ref_summary["dim_customers"]
    ref_prod = ref_summary["dim_products"]

    def g(section: str, key: str) -> Any:
        payload = form.get(section)
        return payload.get(key) if isinstance(payload, dict) else None

    extraction_ok = all(
        g("extraction", k) == v for k, v in RAW_INPUT_COUNTS.items()
    )
    ledger_ok = all(
        g("transformations", k) == v for k, v in RAW_TRANSFORMATION_COUNTS.items()
    )

    fact_ok = (
        within_pct(g("fact_table", "row_count"), ref_fact["row_count"], COUNT_TOLERANCE_PCT)
        and within_pct(g("fact_table", "unique_transactions"),
                       ref_fact["unique_transactions"], COUNT_TOLERANCE_PCT)
        and within_pct(g("fact_table", "unique_customers"),
                       ref_fact["unique_customers"], COUNT_TOLERANCE_PCT)
        and within_pct(g("fact_table", "unique_products"),
                       ref_fact["unique_products"], COUNT_TOLERANCE_PCT)
        and within_pct(g("fact_table", "total_revenue"),
                       ref_fact["total_revenue"], REVENUE_TOLERANCE_PCT)
        and isinstance(g("fact_table", "avg_transaction_value"), (int, float))
        and abs(g("fact_table", "avg_transaction_value")
                - ref_fact["avg_transaction_value"]) <= AVG_TOLERANCE_ABS
        and _norm_date_key(g("fact_table", "fact_table_date_range_min")) == ref_fact["date_range"]["min"]
        and _norm_date_key(g("fact_table", "fact_table_date_range_max")) == ref_fact["date_range"]["max"]
        and _dist_close(g("fact_table", "channel_distribution"),
                        ref_fact["channel_distribution"], COUNT_TOLERANCE_PCT)
    )

    dims_ok = (
        within_pct(g("dim_customers", "row_count"), ref_cust["row_count"], COUNT_TOLERANCE_PCT)
        and _dist_close(g("dim_customers", "segment_distribution"),
                        ref_cust["segment_distribution"], COUNT_TOLERANCE_PCT)
        and _dist_close(g("dim_customers", "country_distribution"),
                        ref_cust["country_distribution"], COUNT_TOLERANCE_PCT)
        and within_pct(g("dim_products", "row_count"), ref_prod["row_count"], COUNT_TOLERANCE_PCT)
        and within_pct(g("dim_products", "active_products"),
                       ref_prod["active_products"], COUNT_TOLERANCE_PCT)
    )

    try:
        arithmetic_ok = (
            g("extraction", "raw_transaction_rows")
            - g("transformations", "duplicates_removed")
            - g("transformations", "null_rows_dropped")
            == g("fact_table", "row_count")
            and abs(g("fact_table", "avg_transaction_value")
                    - g("fact_table", "total_revenue") / g("fact_table", "row_count")) <= 0.01
            and sum(g("fact_table", "channel_distribution").values())
            == g("fact_table", "row_count")
            and sum(g("dim_customers", "segment_distribution").values())
            == g("dim_customers", "row_count")
            and sum(g("dim_customers", "country_distribution").values())
            == g("dim_customers", "row_count")
        )
    except (TypeError, AttributeError, ZeroDivisionError):
        arithmetic_ok = False

    decision = form.get("decision")
    if decision == "approved":
        decision_ok = engineer_reconciles
    elif decision == "rejected":
        # A reject is well-founded iff the build genuinely fails AND the
        # filed figures are an accurate audit: internally consistent and,
        # when the actual build is measurable, matching it within the same
        # tolerances.
        accurate_vs_build = True
        if candidate_metrics is not None and "error" not in candidate_metrics:
            accurate_vs_build = (
                within_pct(g("fact_table", "row_count"),
                           candidate_metrics["fact_rows"], COUNT_TOLERANCE_PCT)
                and within_pct(g("fact_table", "total_revenue"),
                               candidate_metrics["total_revenue"], REVENUE_TOLERANCE_PCT)
            )
        decision_ok = (not engineer_reconciles) and arithmetic_ok and accurate_vs_build
    else:
        decision_ok = False

    criteria = {
        "extraction_ledger_correct": extraction_ok,
        "transformation_ledger_correct": ledger_ok,
        "fact_metrics_within_tolerance": bool(fact_ok),
        "dim_metrics_within_tolerance": bool(dims_ok),
        "arithmetic_consistent": bool(arithmetic_ok),
        "decision_correct": bool(decision_ok),
    }
    score = sum(1.0 for ok in criteria.values() if ok) / len(criteria)
    return score, {"criteria": criteria, "decision": decision}


def load():
    return load_tasks(__file__)


# ============================================================================
# Lifecycle hooks
# ============================================================================

_ENGINEER_STAGED_SPOT_CHECKS = (
    "input/raw_transactions/transactions_2024_01.csv",
    "input/raw_customers/customers.json",
    "input/raw_products/product_catalog.tsv",
    "software/inspect_extracts.sh",
)
_GOVERNANCE_STAGED_SPOT_CHECKS = (
    "input/governance_standard_dg7.md",
    "input/target_schema_spec.json",
    "input/release_signoff_form.json",
    "software/check_signoff_form.py",
)


async def start(task_cfg, sandboxes):
    """Assert per-role staging landed; ready both output dirs."""
    cfg = task_config(__file__)
    for role, rel_paths in (
        ("engineer", _ENGINEER_STAGED_SPOT_CHECKS),
        ("governance", _GOVERNANCE_STAGED_SPOT_CHECKS),
    ):
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
        # Exec bits are not preserved by the staging transport.
        await sb.run_command(f"chmod +x {shlex.quote(cfg.software_dir)}/* || true")

    await sandboxes["engineer"].rm([
        cfg.warehouse_db_path, cfg.quality_report_path, cfg.summary_path,
    ])
    await sandboxes["governance"].rm([cfg.signoff_path])
    logger.info("[h1_sales_warehouse] staged inputs verified; output dirs ready")


async def _metrics_in_sandbox(sandbox, db_path: str) -> dict[str, Any] | None:
    """Run METRICS_SCRIPT inside the sandbox against ``db_path``.

    Returns the parsed metrics dict, or None when the db is missing or the
    extraction fails (the JSON error payload is logged). Runs post-run, so
    the script is never agent-visible.
    """
    if not await sandbox.exists(db_path):
        logger.info("[h1_sales_warehouse] no database at %s", db_path)
        return None
    script_path = "/tmp/dg7_metrics.py"
    await sandbox.write_file(script_path, METRICS_SCRIPT)
    result = await sandbox.run_command(
        f"python3 {script_path} {shlex.quote(db_path)}", timeout=300,
    )
    try:
        payload = json.loads((result.stdout or "").strip())
    except json.JSONDecodeError:
        logger.info(
            "[h1_sales_warehouse] metric extraction unparseable for %s: rc=%s "
            "stdout=%r stderr=%r", db_path, result.returncode,
            (result.stdout or "")[:200], (result.stderr or "")[:200],
        )
        return None
    if "error" in payload:
        logger.info("[h1_sales_warehouse] metric extraction failed for %s: %s",
                    db_path, payload["error"])
        return None
    return payload


async def _read_json(sandbox, path: str) -> dict[str, Any] | None:
    try:
        return json.loads(await sandbox.read_text(path))
    except FileNotFoundError:
        logger.info("[h1_sales_warehouse] missing: %s", path)
        return None
    except (json.JSONDecodeError, RuntimeError) as exc:
        logger.info("[h1_sales_warehouse] unparseable JSON at %s: %s", path, exc)
        return None


async def evaluate(task_cfg, sandboxes) -> dict[str, Any]:
    """Score both sandboxes; 0.5*engineer + 0.5*governance, with its rubric."""
    cfg = task_config(__file__)
    eng = sandboxes["engineer"]
    gov = sandboxes["governance"]

    # ─── engineer side ───
    reference_metrics = await _metrics_in_sandbox(eng, cfg.reference_db_path)
    if reference_metrics is None:
        raise RuntimeError(
            f"reference warehouse unreadable at {cfg.reference_db_path} in the "
            "engineer sandbox — reference staging failed."
        )
    candidate_metrics = await _metrics_in_sandbox(eng, cfg.warehouse_db_path)
    report = await _read_json(eng, cfg.quality_report_path)
    summary = await _read_json(eng, cfg.summary_path)
    engineer_score, engineer_detail = score_engineer(
        candidate_metrics, reference_metrics, report, summary,
    )
    engineer_reconciles = engineer_score == 1.0

    # ─── governance side ───
    ref_summary = await _read_json(gov, cfg.reference_summary_path)
    if ref_summary is None:
        raise RuntimeError(
            f"reference summary unreadable at {cfg.reference_summary_path} in "
            "the governance sandbox — reference staging failed."
        )
    form = await _read_json(gov, cfg.signoff_path)
    governance_score, governance_detail = score_governance(
        form, ref_summary, engineer_reconciles, candidate_metrics,
    )

    logger.info("[h1_sales_warehouse] engineer subscore=%.4f detail=%s",
                engineer_score, engineer_detail)
    logger.info("[h1_sales_warehouse] governance subscore=%.4f detail=%s",
                governance_score, governance_detail)
    combined = 0.5 * engineer_score + 0.5 * governance_score
    logger.info("[h1_sales_warehouse] combined score=%.4f "
                "(engineer=%.4f, governance=%.4f)",
                combined, engineer_score, governance_score)
    return scored(combined, [
        part("engineer", engineer_score, weight=0.5, **engineer_detail),
        part("governance", governance_score, weight=0.5, **governance_detail),
    ])
