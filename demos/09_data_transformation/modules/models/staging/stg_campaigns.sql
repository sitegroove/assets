WITH source AS (
    SELECT * FROM {{ source('landing_google_ads', 'campaigns') }}
),

cleaned AS (
    SELECT
        campaign_id,
        TRIM(campaign_name) AS campaign_name,
        {{ cents_to_dollars('budget_amount') }} AS daily_budget,
        CASE WHEN status = 'ENABLED' THEN TRUE ELSE FALSE END AS is_active,
        start_date AS started_at
    FROM source
)

SELECT * FROM cleaned
