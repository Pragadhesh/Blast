-- baseline version (current main branch)
select
    order_id,
    customer_id,
    order_status,
    order_total,
    order_total_tax,
    order_date
from {{ source('ecommerce', 'orders') }}
