select
    o.order_id,
    o.customer_id,
    o.order_status,
    o.order_total,
    o.order_total_tax,
    o.order_date,
    c.customer_name
from {{ ref('stg_orders') }} o
left join {{ ref('stg_customers') }} c
    on o.customer_id = c.customer_id
