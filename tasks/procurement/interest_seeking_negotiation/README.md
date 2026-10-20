# Interest-seeking negotiation

## Scientific question

The project asks how agents balance negotiation efficiency with the interests
of their principals. This task isolates a stage mechanism: when authority and
minimum completion conditions are explicit but the direction and magnitude of
principal value must be recovered from organizational evidence, does an agent
stop at a valid deal or continue to improve its principal's outcome?

The contribution claim is conditional. A richer procurement story or utility
formula is not enough. Evidence must separate objective recognition,
evidence-to-action failure, feasibility satisficing, authority-risk
conservatism, and counterparty adaptation.

## Scenario

Cedar Labs and HarborDesk negotiate a 60-seat, one-year agreement. Price and
five non-price issues are negotiable: guided migration, priority onboarding,
premium incident response, annual versus quarterly billing, and a narrowly
limited anonymized reference right. Each side derives its own BATNA, reference
point, and issue values from private files; neither receives the other's values
or a complete utility table.

The all-issue package creates the authored maximum joint BATNA gain of USD
15,100, but the USD 23,000 buyer authority ceiling produces multiple feasible
distributions and Pareto-frontier packages. This makes the task genuinely
integrative rather than a disguised single-price bargain.

Each organization keeps its private analysis and source material locally.
Cross-organization filing confirmation exchanges only the customer-visible
`purchase_record.json` and `supplier_record.json`, or their exact contents.

This drop ships the `base` variant only: both sides recover principal value
from private evidence, without an explicit “seek the strongest reasonably
attainable principal value” instruction.

## Scoring: L1 is not Final Score

A run first passes a validity gate (V): matching final records, hard terms,
complete filings, buyer authority and package-specific individual rationality,
and seller package-specific individual rationality. Invalid or avoidable
no-deal outcomes receive zero L1 and Final Score.

For valid agreements:

```text
execution (E) = mean(buyer_execution, seller_execution)
  role_execution = 0.50 * private-analysis quality + 0.50 * filing quality
JUR = joint_gain / 15,100
W   = behavior multiplier from DPE / FPD / SR (N/A items excluded)

l1_score    = V · E · JUR
final_score = V · E · JUR · W     # alias: composite_score (deprecated)
```

`l1_score` is paper L1. `final_score` / `composite_score` is the scenario-side
composite and **must not be labeled L1**. Buyer/Seller Utility Realization and
Principal Balance remain descriptive and do not enter either scalar.

The framework top-level task scalar remains JUR (normalized joint BATNA gain)
for harness compatibility. Role parts in `main.py` stay 20% execution + 80%
normalized own BATNA gain; those role parts are not the paper L1 scalar.

Price, issue choices, raw BATNA gains, uncreated joint value, process
measures, token use, and estimated dollars are diagnostics rather than extra
score components. Cost is outside both L1 and the optional repo L2 interaction
judge. The interaction judge is not claimed as L2 in this drop.

Process diagnostics include whether the first feasible formal offer was Pareto
efficient and the buyer, seller, and joint utility changes from that offer to
the final accepted offer. Observable post-feasibility behavior (later offers,
package versus price-only moves, infeasible offers, self-advancing offers) is
kept separate from L1.

## Files and commands

- `main.py`: deterministic validity/execution/JUR facts, setup, and evaluation.
- `analyze.py`: offline process metrics; writes `l1_score` and `final_score`.
- `run_and_report.py`: runs one experiment and prints combined cost afterward.

`<exp>.yaml` is an experiment yaml (adapt `example_exp_a2a.yaml`) binding the
`buyer` and `seller` roles, with
`tasks: selected_tasks/interest_seeking_negotiation_base.yaml`.

No-cost validation:

```bash
uv run python -m a2_run run <exp>.yaml --dry-run
```

Paid runs must use the cost-reporting wrapper. Before every paid run or batch,
state its condition, purpose, expected dollar range, and obtain approval:

```bash
uv run python -m tasks.procurement.interest_seeking_negotiation.run_and_report \
  <exp>.yaml \
  -S name=unique_run_name
```

The wrapper prints combined buyer-plus-seller input, output, cache-read, and
cache-creation tokens plus an estimated API cost. Provider billing remains
authoritative.
