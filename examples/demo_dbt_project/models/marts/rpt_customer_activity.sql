-- 2-hop downstream (via int_orders_enriched); untouched by either change
select
    customer_id,
    customer_name,
    count(order_id) as order_count,
    max(order_date) as last_order_date
from {{ ref('int_orders_enriched') }}
group by 1, 2
