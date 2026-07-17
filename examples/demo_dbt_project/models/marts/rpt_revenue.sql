-- direct downstream of stg_orders; aggregates order_total_tax, sensitive to precision loss
select
    date_trunc('month', order_date) as revenue_month,
    sum(order_total_tax) as total_tax_collected
from {{ ref('stg_orders') }}
group by 1
