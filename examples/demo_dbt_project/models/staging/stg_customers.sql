select
    customer_id,
    customer_name,
    signup_date
from {{ source('ecommerce', 'customers') }}
