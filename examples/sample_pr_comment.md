<!--
This is real output from `python agents/blast-scan/main.py --demo`
(BLAST_MOCK_DATAHUB=1, BLAST_MOCK_LLM=1), run against
examples/demo_dbt_project vs examples/demo_pr_change. See
examples/demo_pr_change/CHANGE_SCENARIO.md for the change that produced it.
-->

<!-- blast-report:stg_orders -->
### 🧨 **Blast** found 2 breaking changes and 1 silent risk downstream in `stg_orders`

Changing `stg_orders` (renamed `order_total` -> `total_amount`, changed `order_total_tax numeric(10,2)` -> `order_total_tax integer`) affects 5 downstream model(s). 2 will break outright: int_orders_enriched, rpt_daily_order_value. 1 carry silent risk: rpt_revenue.

📜 **Institutional memory:** `stg_orders` has been flagged for 3 predicted breaking changes in the last 90 days (risk score: 100/100) -- this isn't a one-off. (Flagged pre-merge, not confirmed shipped breakage.)

👤 **Owners to loop in:** `int_orders_enriched` (data-eng), `rpt_revenue` (finance-analytics), `rpt_daily_order_value` (ops-analytics)

#### Downstream impact

| | Model | Hops | Verdict |
|---|---|---|---|
| 🔴 | `int_orders_enriched` | 1 | hard break |
| 🔴 | `rpt_daily_order_value` | 2 | hard break |
| 🟡 | `rpt_revenue` | 1 | silent risk |
| 🟢 | `fct_orders` | 1 | safe |
| 🟢 | `rpt_customer_activity` | 2 | safe |

#### Details

- 🔴 **`int_orders_enriched`**
  - references `order_total`, which was renamed to `total_amount` upstream and no longer exists.
  - selects `order_total_tax`, whose type is changing from numeric(10,2) to integer -- downstream consumers may see different formatting/precision.
- 🟡 **`rpt_revenue`**
  - aggregates/computes over `order_total_tax`, whose type is changing from numeric(10,2) to integer -- may silently change results (e.g. precision loss) without erroring.
- 🔴 **`rpt_daily_order_value`**
  - references `order_total`, which was renamed to `total_amount` upstream and no longer exists.

#### Dependency graph

```mermaid
graph LR
    stg_orders["stg_orders"]:::changed
    int_orders_enriched["int_orders_enriched"]:::hard_break
    fct_orders["fct_orders"]:::safe
    rpt_revenue["rpt_revenue"]:::silent_risk
    rpt_daily_order_value["rpt_daily_order_value"]:::hard_break
    rpt_customer_activity["rpt_customer_activity"]:::safe
    int_orders_enriched -->|safe| rpt_customer_activity
    int_orders_enriched -->|hard break| rpt_daily_order_value
    stg_orders -->|safe| fct_orders
    stg_orders -->|hard break| int_orders_enriched
    stg_orders -->|silent risk| rpt_revenue
    classDef hard_break fill:#ffdce0,stroke:#d1242f,stroke-width:2px,color:#1f2328
    classDef silent_risk fill:#fff8c5,stroke:#9a6700,stroke-width:2px,color:#1f2328
    classDef safe fill:#dafbe1,stroke:#1a7f37,stroke-width:2px,color:#1f2328
    classDef needs_review fill:#ffe5b4,stroke:#bc4c00,stroke-width:2px,color:#1f2328
    classDef changed fill:#d0d7ff,stroke:#4c2889,stroke-width:2px,color:#1f2328
```

_Findings written back to DataHub as an incident on the changed dataset. Comment `/blast-fix` on this PR to have **Splint** propose a fix as a follow-up PR._
