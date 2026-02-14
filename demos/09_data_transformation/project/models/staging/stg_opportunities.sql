WITH source AS (
    SELECT * FROM {{ source('fivetran_salesforce', 'opportunities') }}
),

cleaned AS (
    SELECT
        opportunity_id,
        account_id,
        TRIM(opportunity_name) AS opportunity_name,
        COALESCE(amount, 0) AS amount_usd,
        stage,
        CASE WHEN stage = 'Closed Won' THEN TRUE ELSE FALSE END AS is_won,
        close_date,
        created_date AS created_at
    FROM source
)

SELECT * FROM cleaned
