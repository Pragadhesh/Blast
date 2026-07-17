-- 2-hop downstream (via int_orders_enriched); still references order_total by name
select
    order_date,
    sum(order_total) as daily_order_value
from {{ ref('int_orders_enriched') }}
group by 1
