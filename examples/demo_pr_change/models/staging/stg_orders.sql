-- proposed PR version: renames order_total -> total_amount
select
    order_id,
    customer_id,
    order_status,
    order_total as total_amount,
    order_total_tax,
    order_date
from {{ source('ecommerce', 'orders') }}
