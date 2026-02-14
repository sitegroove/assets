WITH source AS (
    SELECT * FROM {{ source('landing_google_ads', 'ad_performance') }}
),

cleaned AS (
    SELECT
        date AS date_day,
        campaign_id,
        impressions,
        clicks,
        CAST(cost_micros AS DOUBLE) / {{ var('cost_micros_divisor') }} AS cost_usd,
        conversions
    FROM source
)

SELECT * FROM cleaned
