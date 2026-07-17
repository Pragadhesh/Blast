# Demo PR: rename + type change on `stg_orders`

This folder holds the "proposed" version of two files from
`examples/demo_dbt_project/models/staging/` — as if a PR branch had modified them.
Feed the old (`demo_dbt_project`) and new (`demo_pr_change`) versions of the same
relative paths into `diff_parser.py` to reproduce the demo end-to-end without a real
GitHub PR or a live DataHub instance (see `BLAST_MOCK_DATAHUB=1` / `lineage_fixture.json`).

## The change

`models/staging/stg_orders.sql` and its `schema.yml`:

| Column           | Before              | After                          | Kind          |
|------------------|---------------------|---------------------------------|---------------|
| `order_total`    | `numeric(10,2)`     | renamed to `total_amount`      | renamed       |
| `order_total_tax`| `numeric(10,2)`     | `integer` (same name)          | type_changed  |
| everything else  | unchanged           | unchanged                       | —             |

## Expected classification (ground truth for testing/demo narration)

| Downstream model          | Hops | Finding                                                              | Verdict        |
|----------------------------|------|-----------------------------------------------------------------------|----------------|
| `int_orders_enriched`      | 1    | selects `order_total` by name, which no longer exists                | 🔴 hard break  |
| `rpt_daily_order_value`    | 2    | selects `order_total` (via `int_orders_enriched`) inside `sum()`       | 🔴 hard break  |
| `rpt_revenue`              | 1    | `sum(order_total_tax)` — column survives but precision silently drops | 🟡 silent risk |
| `fct_orders`               | 1    | never references either changed column                                | 🟢 safe        |
| `rpt_customer_activity`    | 2    | never references either changed column                                | 🟢 safe        |

This gives one hard break at 1 hop, one hard break at 2 hops (proving lineage-aware,
not just adjacent-file, analysis), one silent-risk type change, and two safe nodes —
enough variety to exercise every branch of `impact_simulator.py` in a single PR.
