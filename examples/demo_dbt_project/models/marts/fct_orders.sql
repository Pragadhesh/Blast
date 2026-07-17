-- direct downstream of stg_orders; does not touch order_total / order_total_tax
select
    order_id,
    customer_id,
    order_status,
    order_date
from {{ ref('stg_orders') }}
