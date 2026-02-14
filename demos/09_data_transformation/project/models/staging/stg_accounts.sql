WITH source AS (
    SELECT * FROM {{ source('fivetran_salesforce', 'accounts') }}
),

cleaned AS (
    SELECT
        account_id,
        TRIM(account_name) AS account_name,
        LOWER(industry) AS industry,
        COALESCE(annual_revenue, 0) AS annual_revenue,
        is_active,
        created_date AS created_at
    FROM source
)

SELECT * FROM cleaned
